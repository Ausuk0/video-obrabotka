"""Пакетная обработка видео одним скриптом поверх ffmpeg.

Берёт все ролики из папки input/, обрабатывает выбранным режимом и кладёт результат в output/.
Показывает прогресс и итоговую таблицу «файл — было МБ — стало МБ».
Ошибка в одном файле не останавливает обработку остальных.

Режимы:
    compress   — сжать (под размер в МБ или по качеству CRF)
    cut        — нарезать (по списку отрезков или каждые N секунд)
    vertical   — вертикальное видео 9:16 для Reels/Shorts/TikTok с размытым фоном
    audio      — извлечь звук в mp3
    concat     — склеить ролики в один
    watermark  — добавить текстовый водяной знак

Примеры:
    python video_tool.py compress --target-mb 8
    python video_tool.py cut --every 5
    python video_tool.py cut --segments otrezki.txt
    python video_tool.py vertical
    python video_tool.py audio
    python video_tool.py concat --name itog.mp4
    python video_tool.py watermark --text "@my_channel"

Требуется только Python 3.8+ и ffmpeg/ffprobe в PATH. Сторонние библиотеки не нужны.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts"}

# Общие настройки кодирования: H.264 + AAC в MP4 открываются везде (телефоны, мессенджеры, соцсети).
VIDEO_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO_ENC = ["-c:a", "aac", "-b:a", "160k"]
MP4_FLAGS = ["-movflags", "+faststart"]
MAP_VA = ["-map", "0:v:0", "-map", "0:a:0?"]  # первая видео- и (если есть) первая аудиодорожка

IS_TTY = sys.stdout.isatty()


class ProcessingError(Exception):
    """Понятная пользователю ошибка обработки одного файла."""


# --------------------------------------------------------------------------------------
# ffprobe / ffmpeg
# --------------------------------------------------------------------------------------

@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int = 0
    height: int = 0
    has_video: bool = False
    has_audio: bool = False


def probe(path: Path) -> MediaInfo:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise ProcessingError("файл не читается (повреждён или это не видео)")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float(data.get("format", {}).get("duration") or (video or audio or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise ProcessingError("не удалось определить длительность файла")
    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        has_video=video is not None,
        has_audio=audio is not None,
    )


def require_video(info: MediaInfo) -> None:
    if not info.has_video:
        raise ProcessingError("в файле нет видеодорожки")


def progress_line(label: str, fraction: float, done: bool = False) -> None:
    fraction = max(0.0, min(1.0, fraction))
    if IS_TTY:
        width = 28
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        end = "\n" if done else ""
        print(f"\r    [{bar}] {fraction * 100:5.1f}%  {label}", end=end, flush=True)
    elif done:
        print(f"    [готово] {label}", flush=True)


def run_ffmpeg(args: list[str], total_seconds: float, label: str) -> None:
    """Запускает ffmpeg, рисует прогресс по out_time из -progress, при ошибке бросает ProcessingError."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                                text=True, encoding="utf-8", errors="replace")
        last_draw = 0.0
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.isdigit() and total_seconds > 0:
                now = time.monotonic()
                if now - last_draw > 0.2:
                    progress_line(label, int(value) / 1_000_000 / total_seconds)
                    last_draw = now
        code = proc.wait()
        if code != 0:
            if IS_TTY:
                print()
            err.seek(0)
            lines = [ln for ln in err.read().decode("utf-8", "replace").splitlines() if ln.strip()]
            tail = " | ".join(lines[-2:]) if lines else f"код выхода {code}"
            raise ProcessingError(f"ffmpeg: {tail}")
    progress_line(label, 1.0, done=True)


def escape_filter_value(value: str) -> str:
    """Экранирует строку для значения опции внутри filtergraph (два уровня экранирования ffmpeg).

    Нужно для путей Windows: 'C:/Windows/Fonts/arial.ttf' -> 'C\\\\\\:/Windows/Fonts/arial.ttf'.
    """
    level1 = "".join("\\" + ch if ch in "\\':" else ch for ch in value)
    return "".join("\\" + ch if ch in "\\'[],;" else ch for ch in level1)


def ff_path(path: Path | str) -> str:
    """Путь в виде, удобном для фильтров ffmpeg: абсолютный, с прямыми слэшами, экранированный."""
    return escape_filter_value(str(Path(path).resolve()).replace("\\", "/"))


def mb(size_bytes: float) -> float:
    return size_bytes / (1024 * 1024)


def safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*%\x00-\x1f]+', "_", text).strip(" ._")
    return text[:60]


def parse_time(text: str) -> float:
    """'1:05' -> 65, '00:01:05.5' -> 65.5, '12,5' -> 12.5"""
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"не понимаю время «{text}»")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def fmt_time(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


# --------------------------------------------------------------------------------------
# Режимы. Каждый обрабатывает ОДИН файл и возвращает список созданных файлов.
# --------------------------------------------------------------------------------------

def mode_compress(info: MediaInfo, out_dir: Path, args) -> list[Path]:
    require_video(info)
    src = info.path
    out = out_dir / f"{src.stem}_compressed.mp4"
    vf = []
    if args.height:
        vf = ["-vf", f"scale=-2:'min({args.height},ih)'"]  # уменьшаем только если исходник больше
    audio_kbps = args.audio_kbps if info.has_audio else 0
    audio = ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"] if info.has_audio else ["-an"]

    if args.target_mb:
        if mb(src.stat().st_size) <= args.target_mb and not args.height:
            shutil.copy2(src, out)
            print(f"    файл уже меньше {args.target_mb} МБ — скопирован без пережатия")
            return [out]
        # Битрейт под целевой размер: МБ -> килобиты, ~4% запаса на контейнер.
        total_kbps = args.target_mb * 8 * 1024 * 1024 / 1000 / info.duration * 0.96
        # Цель — это потолок: файл не должен получиться больше исходника.
        total_kbps = min(total_kbps, src.stat().st_size * 8 / 1000 / info.duration * 0.9)
        video_kbps = int(total_kbps - audio_kbps)
        if video_kbps < 100:
            need = (100 + audio_kbps) * 1000 * info.duration / 0.96 / 8 / 1024 / 1024
            raise ProcessingError(f"цель {args.target_mb} МБ слишком мала для {info.duration:.0f} с видео "
                                  f"(нужно хотя бы ~{need:.1f} МБ или уменьшите --height)")
        rate = ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
                "-bufsize", f"{video_kbps * 2}k"]
        base = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", *rate]
        with tempfile.TemporaryDirectory() as tmp:
            passlog = str(Path(tmp) / "pass")
            run_ffmpeg(["-i", str(src), "-map", "0:v:0", *vf, *base, "-pass", "1", "-passlogfile", passlog,
                        "-an", "-f", "mp4", os.devnull],
                       info.duration, f"проход 1/2, видео {video_kbps} кбит/с")
            run_ffmpeg(["-i", str(src), *MAP_VA, *vf, *base, "-pass", "2", "-passlogfile", passlog,
                        *audio, *MP4_FLAGS, str(out)],
                       info.duration, "проход 2/2")
    else:
        run_ffmpeg(["-i", str(src), *MAP_VA, *vf, "-c:v", "libx264", "-preset", "medium",
                    "-crf", str(args.crf), "-pix_fmt", "yuv420p", *audio, *MP4_FLAGS, str(out)],
                   info.duration, f"CRF {args.crf}")
    return [out]


@dataclass
class Segment:
    start: float
    end: float
    title: str = ""


def read_segments(path: Path) -> list[Segment]:
    """Формат строк: «начало - конец [название]», время как 15, 1:05 или 00:01:05.5. # — комментарий."""
    if not path.is_file():
        raise SystemExit(f"Файл с отрезками не найден: {path}")
    segments = []
    for num, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([\d:.,]+)\s*[-–—]\s*([\d:.,]+)\s*(.*)$", line)
        if not m:
            raise SystemExit(f"{path.name}, строка {num}: ожидаю «начало - конец [название]», а вижу «{raw}»")
        try:
            start, end = parse_time(m.group(1)), parse_time(m.group(2))
        except ValueError as exc:
            raise SystemExit(f"{path.name}, строка {num}: {exc}")
        if end <= start:
            raise SystemExit(f"{path.name}, строка {num}: конец отрезка должен быть позже начала")
        segments.append(Segment(start, end, m.group(3).strip()))
    if not segments:
        raise SystemExit(f"В файле {path.name} нет ни одного отрезка")
    return segments


def mode_cut(info: MediaInfo, out_dir: Path, args) -> list[Path]:
    require_video(info)
    src = info.path
    stem = safe_name(src.stem) or "video"
    # --copy: без перекодирования кусок начинается с ближайшего ключевого кадра ДО нужного времени.
    # make_zero сдвигает время к нулю, чтобы все плееры показывали кусок одинаково (без скрытых edit list).
    codec = (["-c", "copy", "-avoid_negative_ts", "make_zero"] if args.copy
             else [*VIDEO_ENC, *AUDIO_ENC])

    if args.every:
        # Равные куски режем так же, как отрезки из списка: каждый — отдельным точным проходом.
        # (Сегментный мюксер ffmpeg режет только по ключевым кадрам и может «склеить» два куска.)
        n = args.every
        segments, t = [], 0.0
        while info.duration - t > 0.05:  # хвост короче 0.05 с не сохраняем
            segments.append(Segment(t, min(t + n, info.duration)))
            t += n
        names = [f"{stem}_part{i:03d}.mp4" for i in range(1, len(segments) + 1)]
    else:
        segments = args.segment_list
        names = []
        for i, seg in enumerate(segments, 1):
            suffix = f"_{safe_name(seg.title)}" if seg.title and safe_name(seg.title) else ""
            names.append(f"{stem}_{i:02d}{suffix}.mp4")

    outputs = []
    for i, (seg, name) in enumerate(zip(segments, names), 1):
        if seg.start >= info.duration:
            print(f"    отрезок {i} ({fmt_time(seg.start)}–{fmt_time(seg.end)}) за пределами ролика "
                  f"({fmt_time(info.duration)}) — пропущен")
            continue
        end = min(seg.end, info.duration)
        out = out_dir / name
        run_ffmpeg(["-ss", f"{seg.start:.3f}", "-i", str(src), "-t", f"{end - seg.start:.3f}",
                    *MAP_VA, *codec, *MP4_FLAGS, str(out)],
                   end - seg.start, f"кусок {i}/{len(segments)}: {fmt_time(seg.start)}–{fmt_time(end)}")
        outputs.append(out)
    if not outputs:
        raise ProcessingError("ни один отрезок не попал в ролик")
    return outputs


def mode_vertical(info: MediaInfo, out_dir: Path, args) -> list[Path]:
    require_video(info)
    w, h = args.width, args.height
    sw, sh = (w // 4) // 2 * 2, (h // 4) // 2 * 2  # фон размываем в уменьшенном виде — в разы быстрее
    graph = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
        f"boxblur=luma_radius={args.blur}:luma_power=2,eq=brightness=-0.08,"
        f"scale={w}:{h},setsar=1[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    out = out_dir / f"{info.path.stem}_vertical.mp4"
    run_ffmpeg(["-i", str(info.path), "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                *VIDEO_ENC, *AUDIO_ENC, *MP4_FLAGS, str(out)],
               info.duration, f"9:16 {w}x{h}")
    return [out]


def mode_audio(info: MediaInfo, out_dir: Path, args) -> list[Path]:
    if not info.has_audio:
        raise ProcessingError("в файле нет звуковой дорожки")
    out = out_dir / f"{info.path.stem}.mp3"
    run_ffmpeg(["-i", str(info.path), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
                "-b:a", args.bitrate, str(out)],
               info.duration, f"mp3 {args.bitrate}")
    return [out]


def default_font() -> str | None:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("arial.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf"):
        candidate = Path(windir) / "Fonts" / name
        if candidate.is_file():
            return str(candidate)
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf",
                      "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if Path(candidate).is_file():
            return candidate
    return None


POSITIONS = {
    "top-left": ("{m}", "{m}"),
    "top-right": ("w-tw-{m}", "{m}"),
    "bottom-left": ("{m}", "h-th-{m}"),
    "bottom-right": ("w-tw-{m}", "h-th-{m}"),
    "center": ("(w-tw)/2", "(h-th)/2"),
}


def mode_watermark(info: MediaInfo, out_dir: Path, args) -> list[Path]:
    require_video(info)
    fontsize = max(12, int(info.height * args.size / 100))
    margin = max(8, int(info.height * 0.03))
    x, y = (part.format(m=margin) for part in POSITIONS[args.position])
    opacity = max(0.0, min(1.0, args.opacity))

    # Текст передаём через textfile, а не text=…: так не нужно экранировать кавычки, двоеточия, % и т.п.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        tf.write(args.text)
        text_path = tf.name
    try:
        opts = [
            f"fontfile={ff_path(args.font)}" if args.font else "font=Sans",
            f"textfile={ff_path(text_path)}",
            "expansion=none",
            f"fontsize={fontsize}",
            f"fontcolor=white@{opacity:.2f}",
            f"shadowcolor=black@{opacity * 0.7:.2f}", "shadowx=2", "shadowy=2",
            f"x={x}", f"y={y}",
        ]
        if args.box:
            opts += ["box=1", f"boxcolor=black@{opacity * 0.5:.2f}", f"boxborderw={max(4, fontsize // 4)}"]
        out = out_dir / f"{info.path.stem}_watermark.mp4"
        run_ffmpeg(["-i", str(info.path), *MAP_VA, "-vf", "drawtext=" + ":".join(opts),
                    *VIDEO_ENC, "-c:a", "copy", *MP4_FLAGS, str(out)],
                   info.duration, f"текст «{args.text}»")
    finally:
        os.unlink(text_path)
    return [out]


# --------------------------------------------------------------------------------------
# Итоговая таблица
# --------------------------------------------------------------------------------------

@dataclass
class Row:
    name: str
    before: float = 0.0
    after: float | None = None
    note: str = ""
    ok: bool = True
    outputs: list[Path] = field(default_factory=list)


def print_table(rows: list[Row], out_dir: Path, elapsed: float) -> None:
    headers = ["Файл", "Было, МБ", "Стало, МБ", "Изменение", "Результат"]
    body = []
    for r in rows:
        after = f"{r.after:.2f}" if r.after is not None else "—"
        if r.after is not None and r.before > 0:
            change = f"{(r.after - r.before) / r.before * 100:+.0f}%"
        else:
            change = "—"
        body.append([r.name, f"{r.before:.2f}", after, change, r.note])
    widths = [max(len(h), *(len(line[i]) for line in body)) for i, h in enumerate(headers)]
    widths[-1] = min(widths[-1], 70)

    def fmt(cells):
        out = []
        for i, c in enumerate(cells):
            c = c if len(c) <= widths[i] else c[: widths[i] - 1] + "…"
            out.append(c.ljust(widths[i]) if i in (0, 4) else c.rjust(widths[i]))
        return "| " + " | ".join(out) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print()
    print(sep)
    print(fmt(headers))
    print(sep)
    for line in body:
        print(fmt(line))
    print(sep)
    ok = sum(r.ok for r in rows)
    failed = [r for r in rows if not r.ok]
    print(f"Успешно: {ok} из {len(rows)}. Время: {elapsed:.1f} с. Результаты: {out_dir}")
    for r in failed:
        print(f"  ОШИБКА {r.name}: {r.note}")


# --------------------------------------------------------------------------------------
# Пакетный запуск
# --------------------------------------------------------------------------------------

def list_inputs(in_dir: Path) -> list[Path]:
    if not in_dir.is_dir():
        raise SystemExit(f"Папка с исходниками не найдена: {in_dir}")
    return sorted(p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def run_batch(handler, files: list[Path], out_dir: Path, args) -> list[Row]:
    rows = []
    for idx, path in enumerate(files, 1):
        print(f"[{idx}/{len(files)}] {path.name}")
        row = Row(path.name, before=mb(path.stat().st_size))
        rows.append(row)
        try:
            info = probe(path)
            outputs = handler(info, out_dir, args)
            row.outputs = outputs
            row.after = sum(mb(p.stat().st_size) for p in outputs)
            row.note = outputs[0].name if len(outputs) == 1 else f"файлов: {len(outputs)} ({outputs[0].name}, …)"
        except ProcessingError as exc:
            row.ok, row.note = False, str(exc)
            print(f"    ОШИБКА: {exc}")
        except Exception as exc:  # неожиданное — тоже не роняем всю пачку
            row.ok, row.note = False, f"{type(exc).__name__}: {exc}"
            print(f"    ОШИБКА: {row.note}")
    return rows


def run_concat(files: list[Path], out_dir: Path, args) -> list[Row]:
    rows: list[Row] = []
    good: list[MediaInfo] = []
    if args.list:
        list_path = Path(args.list)
        names = [ln.split("#", 1)[0].strip() for ln in list_path.read_text(encoding="utf-8-sig").splitlines()]
        files = [(Path(args.input) / n) if not Path(n).is_absolute() else Path(n) for n in names if n]

    print(f"Проверяю файлы для склейки (всего {len(files)}):")
    for path in files:
        try:
            if not path.is_file():
                raise ProcessingError("файл не найден")
            info = probe(path)
            require_video(info)
            good.append(info)
            print(f"    {path.name}: {info.width}x{info.height}, {fmt_time(info.duration)}"
                  f"{'' if info.has_audio else ', без звука (подставлю тишину)'}")
        except ProcessingError as exc:
            rows.append(Row(path.name, before=mb(path.stat().st_size) if path.is_file() else 0,
                            ok=False, note=f"пропущен: {exc}"))
            print(f"    {path.name}: ПРОПУЩЕН — {exc}")
    if len(good) < 2:
        rows.append(Row("склейка", ok=False, note="для склейки нужно хотя бы 2 исправных ролика"))
        return rows

    if args.size:
        w, h = (int(v) for v in args.size.lower().split("x"))
    else:
        w, h = good[0].width, good[0].height
    w, h = w // 2 * 2, h // 2 * 2

    # Каждый ролик приводим к одному размеру/частоте/формату звука — тогда склеиваются любые исходники.
    inputs, parts, chain = [], [], []
    for i, info in enumerate(good):
        inputs += ["-i", str(info.path)]
    extra = len(good)
    for i, info in enumerate(good):
        parts.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={args.fps},format=yuv420p[v{i}]")
        if info.has_audio:
            parts.append(f"[{i}:a:0]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{info.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            parts.append(f"[{extra}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
            extra += 1
        chain.append(f"[v{i}][a{i}]")
    graph = ";".join(parts) + ";" + "".join(chain) + f"concat=n={len(good)}:v=1:a=1[v][a]"

    name = args.name if args.name.lower().endswith(".mp4") else args.name + ".mp4"
    out = out_dir / name
    total = sum(i.duration for i in good)
    row = Row(f"склейка ({len(good)} шт.)", before=sum(mb(i.path.stat().st_size) for i in good))
    try:
        run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    *VIDEO_ENC, *AUDIO_ENC, *MP4_FLAGS, str(out)],
                   total, f"{w}x{h}, {fmt_time(total)}")
        row.after, row.note, row.outputs = mb(out.stat().st_size), out.name, [out]
    except ProcessingError as exc:
        row.ok, row.note = False, str(exc)
        print(f"    ОШИБКА: {exc}")
    rows.append(row)
    return rows


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", "-i", default=str(HERE / "input"),
                        help="папка с исходными роликами (по умолчанию input рядом со скриптом)")
    common.add_argument("--output", "-o", default=str(HERE / "output"),
                        help="папка для результатов (по умолчанию output рядом со скриптом)")

    p = argparse.ArgumentParser(
        prog="video_tool.py",
        description="Пакетная обработка видео из папки input/ в папку output/ (нужен ffmpeg).",
        epilog="Подробная справка по режиму: python video_tool.py <режим> --help",
    )
    sub = p.add_subparsers(dest="mode", metavar="<режим>", required=True)

    c = sub.add_parser("compress", parents=[common], help="сжать (для мессенджеров/почты)",
                       description="Сжать ролики: под нужный размер файла (--target-mb) или по качеству (--crf).")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--target-mb", type=float, help="желаемый размер каждого файла в МБ (двухпроходное сжатие)")
    g.add_argument("--crf", type=int, default=28,
                   help="качество 18 (почти без потерь) … 32 (сильно сжато), по умолчанию 28")
    c.add_argument("--height", type=int, help="уменьшить до этой высоты, например 720 или 480")
    c.add_argument("--audio-kbps", type=int, default=96, help="битрейт звука, кбит/с (по умолчанию 96)")

    k = sub.add_parser("cut", parents=[common], help="нарезать на куски",
                       description="Нарезать ролики по списку отрезков из файла или на равные куски.")
    g = k.add_mutually_exclusive_group(required=True)
    g.add_argument("--segments", help="текстовый файл, в каждой строке «начало - конец [название]»")
    g.add_argument("--every", type=float, help="резать на куски по N секунд")
    k.add_argument("--copy", action="store_true",
                   help="без перекодирования: очень быстро, но границы — по ближайшим ключевым кадрам")

    v = sub.add_parser("vertical", parents=[common], help="вертикальное 9:16 с размытым фоном",
                       description="Сделать вертикальное видео 9:16 (Reels/Shorts/TikTok) с размытым фоном.")
    v.add_argument("--width", type=int, default=1080, help="ширина результата (по умолчанию 1080)")
    v.add_argument("--height", type=int, default=1920, help="высота результата (по умолчанию 1920)")
    v.add_argument("--blur", type=int, default=12, help="сила размытия фона 2…30 (по умолчанию 12)")

    a = sub.add_parser("audio", parents=[common], help="извлечь звук в mp3",
                       description="Извлечь звуковую дорожку каждого ролика в mp3.")
    a.add_argument("--bitrate", default="192k", help="битрейт mp3 (по умолчанию 192k)")

    j = sub.add_parser("concat", parents=[common], help="склеить ролики в один",
                       description="Склеить ролики в один файл (по алфавиту или по списку). "
                                   "Разные разрешения и ролики без звука допускаются.")
    j.add_argument("--name", default="sklejka.mp4", help="имя итогового файла (по умолчанию sklejka.mp4)")
    j.add_argument("--list", help="текстовый файл с именами роликов в нужном порядке (по одному в строке)")
    j.add_argument("--size", help="размер результата, например 1920x1080 (по умолчанию — как у первого ролика)")
    j.add_argument("--fps", type=int, default=30, help="частота кадров результата (по умолчанию 30)")

    w = sub.add_parser("watermark", parents=[common], help="добавить текстовый водяной знак",
                       description="Наложить текстовый водяной знак (поддерживается кириллица).")
    w.add_argument("--text", required=True, help="текст знака, например \"@my_channel\"")
    w.add_argument("--position", choices=list(POSITIONS), default="bottom-right",
                   help="где разместить (по умолчанию bottom-right)")
    w.add_argument("--size", type=float, default=5, help="высота текста в %% от высоты кадра (по умолчанию 5)")
    w.add_argument("--opacity", type=float, default=0.7, help="непрозрачность 0…1 (по умолчанию 0.7)")
    w.add_argument("--box", action="store_true", help="подложить полупрозрачную плашку под текст")
    w.add_argument("--font", default=default_font(),
                   help="путь к файлу шрифта .ttf (по умолчанию C:/Windows/Fonts/arial.ttf)")
    return p


def check_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise SystemExit("Не найдены программы: " + ", ".join(missing) +
                         ". Установите ffmpeg и добавьте папку bin в PATH (см. README.md).")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    check_tools()

    in_dir, out_dir = Path(args.input).resolve(), Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list_inputs(in_dir)
    if not files and not (args.mode == "concat" and args.list):
        print(f"В папке {in_dir} нет видеофайлов ({', '.join(sorted(VIDEO_EXTS))}).")
        return 2

    if args.mode == "cut" and args.segments:
        args.segment_list = read_segments(Path(args.segments))
        print(f"Отрезков в списке: {len(args.segment_list)}")
    if args.mode == "watermark" and args.font and not Path(args.font).is_file():
        raise SystemExit(f"Шрифт не найден: {args.font}")
    if args.mode == "cut" and args.every is not None and args.every <= 0:
        raise SystemExit("--every должно быть больше нуля")

    print(f"Режим: {args.mode}. Исходники: {in_dir}. Результат: {out_dir}")
    started = time.monotonic()
    handlers = {"compress": mode_compress, "cut": mode_cut, "vertical": mode_vertical,
                "audio": mode_audio, "watermark": mode_watermark}
    try:
        if args.mode == "concat":
            rows = run_concat(files, out_dir, args)
        else:
            rows = run_batch(handlers[args.mode], files, out_dir, args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    print_table(rows, out_dir, time.monotonic() - started)
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
