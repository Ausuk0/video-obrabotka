"""Создаёт тестовые ролики в папке input/ средствами самого ffmpeg (ничего не скачивается).

    python make_test_videos.py               # три ролика 1280x720 со звуком
    python make_test_videos.py --with-broken # плюс «битый» файл для проверки устойчивости к ошибкам
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUT = HERE / "input"

# имя файла, длительность (с), частота тона (Гц)
CLIPS = [
    ("01_intro.mp4", 12, 440),
    ("02_main.mp4", 15, 660),
    ("03_final.mp4", 18, 880),
]


def make_clip(path: Path, seconds: int, freq: int) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
        # Высокий битрейт специально: чтобы режиму «сжать» было что сжимать.
        # Ключевой кадр раз в секунду (-g 30), как у видео с телефона.
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "6M", "-g", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать тестовые ролики в input/")
    parser.add_argument("--with-broken", action="store_true",
                        help="добавить повреждённый файл 99_broken.mp4 (для демонстрации обработки ошибок)")
    args = parser.parse_args()

    INPUT.mkdir(exist_ok=True)
    for name, seconds, freq in CLIPS:
        target = INPUT / name
        print(f"Создаю {target.name} ({seconds} с, 1280x720, тон {freq} Гц)...")
        try:
            make_clip(target, seconds, freq)
        except FileNotFoundError:
            print("Не найден ffmpeg. Установите его и добавьте в PATH (см. README.md).")
            return 1
        except subprocess.CalledProcessError as exc:
            print(f"ffmpeg завершился с ошибкой ({exc.returncode}) на {name}")
            return 1

    if args.with_broken:
        broken = INPUT / "99_broken.mp4"
        broken.write_bytes(b"this is not a video file " * 200)
        print(f"Создан повреждённый файл {broken.name}")

    print(f"Готово. Файлы лежат в {INPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
