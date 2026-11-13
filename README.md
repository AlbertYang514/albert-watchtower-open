# Albert Watchtower

A small, dependency-free server watchroom that sends weather, host health and
website health reports by SMTP email, with optional LLM-written notes.

The default report titles keep the playful **小鸡值班室** ("little chicken duty
room") style. Every title, location, website and server label is configurable,
so the project is equally usable as a plain status reporter.

- Python standard library only
- One script, one JSON config, no database, no web UI
- Morning / evening / daily reports
- Open-Meteo weather (no API key)
- Linux host uptime / disk / memory checks
- Website health checks
- Optional OpenAI-compatible LLM observation and suggestion
- Safe for `systemd` timers and cron

## Requirements

- Python 3.8+
- No third-party Python packages

## Quick start

```bash
git clone https://github.com/AlbertYang514/albert-watchtower-open.git
cd albert-watchtower-open
cp config.example.json config.json
chmod 600 config.json
```

Edit `config.json`:

- `email`: SMTP host, port, account and authorization code
- `locations`: one or more places to show in the weather section
- `sites`: websites to health-check
- `report.titles` / `report.server_label`: optional theme customization
- `llm`: optional OpenAI-compatible endpoint

Then preview a report without sending anything:

```bash
python3 watchtower.py --dry-run
python3 watchtower.py --dry-run --report-type morning
```

Send a report:

```bash
python3 watchtower.py
```

`--config` or the `WATCHTOWER_CONFIG` environment variable can point at a
different config file.

## Configuration

### Email

| Field | Description |
|---|---|
| `email.smtp_host` | SMTP server, e.g. `smtp.example.com` |
| `email.smtp_port` | `465` for SSL, `587` for STARTTLS |
| `email.smtp_ssl` | Use `SMTP_SSL` |
| `email.smtp_starttls` | Use STARTTLS when `smtp_ssl` is false |
| `email.username` | SMTP login |
| `email.password` | SMTP authorization code, not usually the mailbox password |
| `email.from` | Sender address |
| `email.to` | Recipient address (comma-separated values are accepted by most SMTP servers) |

### Report theme

```json
"report": {
  "server_label": "小鸡",
  "sites_heading": "网站",
  "titles": {
    "morning": "小鸡值班室早报🐣",
    "evening": "小鸡值班室晚报🐣",
    "daily": "小鸡值班室日报🐣"
  }
}
```

Set any labels you like, or replace the titles with plain text such as
`Morning report`.

### Locations

`locations` can be a list (recommended) or a mapping. Each entry needs `name`,
`lat` and `lon`; `timezone` defaults to the top-level `timezone` value
(`auto` is supported by Open-Meteo).

```json
"locations": [
  { "name": "示例城市 A", "lat": 0.0, "lon": 0.0, "role": "home" },
  { "name": "示例城市 B", "lat": 0.0, "lon": 0.0, "role": "server" }
]
```

### Websites

```json
"sites": [
  { "name": "示例网站", "url": "https://example.com" }
]
```

Each site is checked with `HEAD`, falling back to a ranged `GET`.

### Optional LLM

```json
"llm": {
  "enabled": true,
  "base_url": "https://api.example.com",
  "api_key": "YOUR_LLM_API_KEY",
  "model": "YOUR_MODEL_NAME",
  "timeout_seconds": 12
}
```

The LLM adds `今日观察` and `今日建议` sections. LLM failures never block email
delivery, and SMTP credentials / API keys / email addresses are not included in
the prompt.

## Report types

When `--report-type` is omitted, the type is chosen from local time:

| Local time | Report type |
|---|---|
| 05:00–11:59 | morning |
| 18:00–23:59 | evening |
| other | daily |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use mock data and mock SMTP/HTTP objects. They never send email or
contact external services.

## systemd timer example

Replace `YOUR_USER`, `/opt/albert-watchtower` and the report times with your own
values. This example only writes unit files under `/tmp`; adjust the install
commands to match your system.

```ini
# /etc/systemd/system/watchtower-morning.service
[Unit]
Description=Watchtower morning report
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=YOUR_USER
WorkingDirectory=/opt/albert-watchtower
ExecStart=/usr/bin/python3 /opt/albert-watchtower/watchtower.py --report-type morning
```

```ini
# /etc/systemd/system/watchtower-morning.timer
[Unit]
Description=Run Watchtower morning report

[Timer]
OnCalendar=*-*-* 08:30:00
Persistent=true
Unit=watchtower-morning.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now watchtower-morning.timer
systemctl list-timers --all | grep watchtower
```

An evening report can use the same service/timer pattern with
`--report-type evening`.

## Security notes

- Never commit `config.json`.
- Use an SMTP authorization code, not a mailbox login password.
- Keep `config.json` at mode `600`.
- `config.example.json` contains placeholders only.
- The project has no telemetry and no third-party Python dependencies.

## License

MIT. See [LICENSE](LICENSE).
