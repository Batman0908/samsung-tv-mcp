# Samsung TV MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that lets AI assistants control Samsung Smart TVs (Tizen OS, 2016+) over the local network. No cloud, no SmartThings account required.

> **Early release** — Core functionality works and has been tested on a Samsung UN65TU7000 (2020, Tizen). Not all tools have been exhaustively tested across different TV models and firmware versions. Bug reports and PRs are welcome.

## Features

- **18 tools** for complete TV control via natural language
- **Auto-discovery** — finds Samsung TVs on your network via SSDP
- **Zero config** — no API keys, no cloud, everything runs locally
- **Multi-protocol** — combines WebSocket, REST, UPnP/DLNA, and Wake-on-LAN
- **Persistent auth** — first connection requires TV approval, then uses saved token
- **Auto-reconnect** — recovers from dropped WebSocket connections

## Tools

| Tool | Description |
|------|-------------|
| `tv_discover` | Scan LAN for Samsung Smart TVs |
| `tv_info` | Get TV status (model, IP, power, volume) |
| `tv_power` | Turn TV on (WoL) or off |
| `tv_key` | Send any remote control key press |
| `tv_keys` | Send a sequence of key presses |
| `tv_navigate` | Semantic navigation (home, back, source, menu...) |
| `tv_volume` | Get/set volume (0-100), mute/unmute |
| `tv_channel` | Change channel by number or up/down |
| `tv_apps` | List all installed apps |
| `tv_launch` | Launch app by name ("netflix") or ID |
| `tv_close_app` | Close a running app |
| `tv_current_app` | Detect which app is currently running |
| `tv_aspect_ratio` | Get/set aspect ratio (Default, 16:9, Zoom, 4:3) |
| `tv_captions` | Get caption state or toggle subtitles |
| `tv_browser` | Open a URL in the TV's browser |
| `tv_text` | Type text into active input fields |
| `tv_cursor` | Move virtual cursor on screen |
| `tv_media` | Play media via DLNA (video/audio/images) with transport controls |

## Requirements

- Python 3.10+
- Samsung Smart TV with Tizen OS (2016 or newer)
- TV and MCP server on the same local network

## Installation

```bash
pip install "samsungtvws[encrypted]" "mcp[cli]<2"
```

`mcp` 2.x renamed `FastMCP`, so `main.py` needs `mcp` 1.x. You can also install the project itself: `pip install -e .`

### Configuration (optional)

| Variable | Purpose |
|----------|---------|
| `SAMSUNG_TV_IP` | Skip SSDP discovery and use this IP (useful when multicast is blocked) |
| `SAMSUNG_TV_MAC` | MAC address for Wake-on-LAN. Otherwise it's learned from `tv_info` and saved to `tv_state.json` |

### Claude Code

```bash
claude mcp add samsung-tv -- python /path/to/samsung-tv-mcp/main.py
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "samsung-tv": {
      "command": "python",
      "args": ["/path/to/samsung-tv-mcp/main.py"]
    }
  }
}
```

## First Connection

On the first WebSocket connection, the TV will show a popup asking to allow "ClaudeCode". Accept it once — a token is saved to `token.json` for future sessions.

To minimize prompts, go to **Settings > General > External Device Manager > Device Connection Manager** and set **Access Notification** to "First Time Only".

## Web Remote

`remote.py` serves a liquid-glass, Apple TV-style home screen and remote that you can open on a phone, a laptop or the TV's own browser:

```bash
python remote.py            # prints http://<this-computer>:8765
```

It shows the apps installed on your TV (tap one to open it there), a D-pad, Home/Back, volume, mute, play/pause, source and power, and marks the app that is currently playing. It listens on your local network with no login, so only run it on a network you trust (use `--host 127.0.0.1` to keep it to this computer).

## TV App (Glass Home)

`tizen-app/` packages the same home screen as a Samsung TV app. On the TV it lists and opens apps through the TV's own API (no server needed) and shows the weather (Open-Meteo, no account).

1. Install the [Tizen Studio CLI](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/installing-tv-sdk.html) plus the Samsung Certificate Extension and TV Extensions (Apple Silicon Macs need Rosetta 2).
2. Turn on Developer Mode on the TV (Apps → type `12345` → On, Host PC IP = your computer) and restart the TV.
3. `sdb connect <tv-ip>`, then create a **Samsung** TV certificate profile in Certificate Manager (it picks up the TV's DUID).
4. `./tizen-app/build.sh <profile> <tv-ip>`

Samsung only lets partner apps start on their own, so `python remote.py --open-on-tv LqGlassTV1.GlassHome` watches the TV and opens Glass Home each time it turns on (it retries while the TV boots, typically 30–40s).

## Supported App Aliases

You can use friendly names instead of numeric IDs:

`netflix`, `youtube`, `prime`, `disney`, `spotify`, `apple tv`, `hbo`, `max`, `plex`, `browser`, `steam link`, `twitch`, `tiktok`, `tubi`, `pluto`, `paramount`, `gallery`, `smartthings`

## Architecture

```
main.py      →  FastMCP tools (thin layer)
tv.py        →  SamsungTV client (WebSocket + UPnP + SSDP + WoL)
remote.py    →  Web remote server (stdlib HTTP, JSON API over tv.py)
remote.html  →  Web remote UI
```

Apps ship under different IDs per model year, so `tv_launch` probes which alias ID is actually installed (cached in `tv_state.json` for 24h; `tv_apps(refresh=True)` rescans). The TV's REST server times out under parallel requests, so all calls to the TV are serialized.

- **WebSocket** (port 8002/8001): remote keys, apps, browser, text input, cursor
- **REST** (port 8001): device info, app launch/close
- **UPnP SOAP** (port 9197): precise volume control, DLNA media playback
- **Wake-on-LAN**: power on from standby

## Known Limitations

- Wake-on-LAN may not work reliably on all models (requires TV config)
- No direct HDMI source switching via API (must navigate the source menu with keys)
- No brightness/contrast/picture settings control
- No screenshot capability
- Text input only works when the TV's virtual keyboard is active
- DRM-protected content cannot be streamed via DLNA
- `tv_browser` does nothing on 2020 TVs (tested on a TU7000): the browser is a store app ("Internet", `3201907018784`) that ignores URLs sent to it

## Tests

```bash
python -m unittest discover -s tests
```

## Tested On

- Samsung UN65TU7000KXZL (2020, Crystal UHD, Tizen, Colombia)
- Samsung UN65TU7000FXZA (2020, Crystal UHD, Tizen 5.5, US)

## License

MIT
