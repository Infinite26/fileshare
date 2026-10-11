# fileshare — a tiny self-hosted file drop (stdlib only)

A single-file Python web server for sharing files on your LAN or over the
internet behind a tunnel. **Zero dependencies** — only the Python standard
library (3.11+).

## Features

- Drag-and-drop uploads and a browsable file list
- Optional access code (`DROPBOX_CODE`) to lock the whole thing
- Optional "inbox" folder watched for new files (e.g. a phone-sync directory)
  with a custom label
- Configurable max upload size and bind address

## Requirements

- Python 3.11+ (nothing else)

## Usage

```bash
./run.sh
# or
python3 server.py
```

Then open `http://<host>:8080`.

### Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `DROPBOX_PORT` | `8080` | Listen port |
| `DROPBOX_HOST` | `0.0.0.0` | Bind address |
| `DROPBOX_CODE` | *(empty)* | Access code; empty = open |
| `DROPBOX_MAX_BYTES` | `20 GiB` | Max upload size |
| `DROPBOX_INBOX` | `./inbox` | Extra watched folder |
| `DROPBOX_INBOX_LABEL` | inbox path | Display label for that folder |
| `DROPBOX_SCAN_INTERVAL` | `3` | Inbox rescan seconds |

```bash
DROPBOX_CODE=hunter2 DROPBOX_PORT=9000 python3 server.py
```

`uploads/` and `inbox/` contents are git-ignored (their `.gitkeep` files are kept).
