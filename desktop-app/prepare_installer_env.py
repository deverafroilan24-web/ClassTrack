"""Set safe defaults in a private installer copy of the camera runtime config."""

from pathlib import Path
import sys


def enable_dashboard_follow(path: Path) -> None:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("Camera runtime configuration is missing")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    lines = [
        line for line in lines
        if line.split("=", 1)[0].strip() not in {"CAMERA_FOLLOW_DASHBOARD", "EDGE_API_KEY"}
    ]
    lines.append("CAMERA_FOLLOW_DASHBOARD=true")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: prepare_installer_env.py <private-runtime-env>")
    enable_dashboard_follow(Path(sys.argv[1]))
