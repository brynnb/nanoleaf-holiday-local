# Nanoleaf Holiday Local

Set up Nanoleaf Holiday String Lights and save a custom color pattern from Linux, without the Nanoleaf app. The pattern stays on the lights. Home Assistant can select it later without leaving this tool running.

**Tested hardware:** NL71K2, firmware 5.2.59. This tool handles one light per local profile. Other models and firmware aren't supported in this release.

## Install

From a Linux computer on the same network, clone this repo and use the system Python. On Debian or Ubuntu:

```bash
sudo apt install python3-cryptography python3-dbus python3-gi avahi-daemon
git clone https://github.com/brynnb/nanoleaf-holiday-local.git
cd nanoleaf-holiday-local
python3 -m nanoleaf_holiday_local doctor
```

## Lights already on Wi-Fi

Don't factory-reset working lights just to install a pattern. Find them and enable the local API:

```bash
python3 -m nanoleaf_holiday_local discover
python3 -m nanoleaf_holiday_local enable-api --host LIGHT_IP
python3 -m nanoleaf_holiday_local install-scene
```

Replace `LIGHT_IP` with your light's local IPv4 address. If discovery doesn't find it, check your router. Confirm the target, then enter the **original printed 11-digit setup code** when prompted. A temporary Matter sharing code won't work here.

The included `Classic Christmas - Static` pattern repeats red, blue, green, amber, and pink. To use your own palette, copy `classic-christmas.json`, change the name and colors, and pass `--palette PATH`. Use `FE` instead of `FF` in any RGB channel: the static scene format treats `FF` as a skip marker. The tool backs up existing scenes locally and refuses to overwrite one with the same name.

## Pair a new or already-reset light

This step needs Bluetooth with BlueZ, npm and a supported Node.js version (20.19+ in the 20.x series, or 22.13+). Matter may contact the public certificate ledger to verify the device. No Nanoleaf account is needed.

```bash
npm ci --ignore-scripts --omit=optional
ip -brief link
python3 -m nanoleaf_holiday_local pair --interface WIFI_INTERFACE
```

Replace `WIFI_INTERFACE` with the interface on the network the lights are joining. Use a 2.4 GHz personal Wi-Fi network. Keep the controller within Bluetooth range. The tool asks for the setup code, Wi-Fi name, and password, then validates the device certificate before sending Wi-Fi credentials. It does not factory-reset the lights. When pairing finishes, follow **Lights already on Wi-Fi** above. If you're restoring a light this tool managed before, use `enable-api --fresh` after a reset.

If an unpaired light stops advertising over Bluetooth, try unplugging it briefly and plugging it back in. If pairing errors out, check whether the light joined Wi-Fi before retrying. A factory reset clears pairings and can remove saved scenes.

## Home Assistant (optional)

Use the [built-in Nanoleaf integration](https://www.home-assistant.io/integrations/nanoleaf/), not a custom component. Start adding the light (or reauthorizing its existing entry after a reset). When Home Assistant asks you to authorize, run `python3 -m nanoleaf_holiday_local link-ha` and complete the prompt within 30 seconds. Pick the saved pattern from the light's **Effect** menu, or use it in a `light.turn_on` automation.

The tool keeps its device ID, tokens, Matter controller state, and scene backups under `~/.local/state/nanoleaf-holiday-local` (or `$XDG_STATE_HOME/nanoleaf-holiday-local`). Don't share that directory. The printed setup code and Wi-Fi password aren't saved by the CLI. The OpenAPI uses local HTTP; use a trusted LAN. Local API setup and static scenes use firmware-specific behavior, so updates may change it.
