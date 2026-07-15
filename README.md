# BMS Seat Check

Checks BookMyShow for matching seats and sends a Telegram message only when seats are available.

## Files

- `bms_withsoldout.py` - seat checker script
- `config.json` - your private runtime config
- `.github/workflows/bms-seat-check.yml` - GitHub Actions workflow

## Setup

1. Copy `config.example.json` to `config.json`.
2. Edit `config.json` with your BookMyShow URL, showtime, row range, and seat range.
3. In GitHub, add repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_USER_ID`
4. Enable GitHub Actions.

GitHub scheduled workflows run at a shortest interval of 5 minutes, not 3 minutes.
