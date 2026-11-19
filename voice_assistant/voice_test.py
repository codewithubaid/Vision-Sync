"""Kept so older commands still work:  python voice_test.py  ==  python voice_service.py"""
from voice_service import (  # noqa: F401
    get_voice_status, is_end_chat, is_exit_command, main, start_voice_service, stop_voice_service,
)

if __name__ == "__main__":
    main()
