"""
Thin entrypoint kept at repo root for backward compatibility with the existing
systemd unit (ExecStart=/home/ubuntu/coc-tracker/venv/bin/python bot.py).

All logic lives in the `coc_tracker` package. See coc_tracker/main.py.
"""

from coc_tracker.main import main

if __name__ == "__main__":
    main()
