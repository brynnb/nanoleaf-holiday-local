# Nanoleaf Holiday Local

Set up Nanoleaf Holiday String Lights from Linux without the Nanoleaf app. The tool saves custom color patterns directly on the controller. Home Assistant can select them after the tool exits.

**Supported hardware:** One NL71K2 controller per local profile, running firmware 5.2.59.

## Install

From a Linux computer on the same network, clone this repo and use the system Python. On Debian or Ubuntu:

```bash
sudo apt install python3-cryptography python3-dbus python3-gi avahi-daemon
git clone https://github.com/brynnb/nanoleaf-holiday-local.git
cd nanoleaf-holiday-local
python3 -m nanoleaf_holiday_local doctor
```

## Lights already on Wi-Fi

Find your light, enable its local API, and install the included pattern:

```bash
python3 -m nanoleaf_holiday_local discover
python3 -m nanoleaf_holiday_local enable-api --host LIGHT_IP
python3 -m nanoleaf_holiday_local install-scene
```

Replace `LIGHT_IP` with your light's local IPv4 address. Check your router's device list if discovery doesn't find it. Confirm the device and enter the **original printed 11-digit setup code** when prompted.

`Classic Christmas - Static` repeats red, blue, green, amber, and pink. For a custom pattern, copy `classic-christmas.json`, edit the scene name and colors in the copy, and pass `--palette PATH` to `install-scene`. Use `FE` instead of `FF` in any RGB channel because `FF` is reserved as a skip marker. The tool backs up existing scenes before saving.

## Pair a new or already-reset light

Bluetooth pairing requires BlueZ, npm, and Node.js 20.19+ in the 20.x series, or 22.13+. Matter may contact the public certificate ledger to verify the device.

```bash
npm ci --ignore-scripts --omit=optional
ip -brief link
python3 -m nanoleaf_holiday_local pair --interface WIFI_INTERFACE
```

Replace `WIFI_INTERFACE` with the interface on the network the lights are joining. Use a 2.4 GHz personal Wi-Fi network and keep the controller within Bluetooth range. Enter the printed setup code, Wi-Fi name, and password when prompted. Once pairing finishes, follow **Lights already on Wi-Fi**. If you're restoring a light this tool managed before, use `enable-api --fresh`.

If an unpaired light stops advertising over Bluetooth, try unplugging it briefly and plugging it back in. If pairing errors out, check whether the light joined Wi-Fi before retrying. A factory reset clears pairings and can remove saved scenes.

## Home Assistant (optional)

Add the light through Home Assistant's [Nanoleaf integration](https://www.home-assistant.io/integrations/nanoleaf/). If you're restoring a light after a reset, reauthorize its existing entry. When Home Assistant asks you to authorize, run `python3 -m nanoleaf_holiday_local link-ha` and finish setup within 30 seconds. Select the saved pattern from the light's **Effect** menu or use it in a `light.turn_on` automation.

## Local state

The tool saves the device ID, tokens, Matter controller state, and scene backups under `~/.local/state/nanoleaf-holiday-local` (or `$XDG_STATE_HOME/nanoleaf-holiday-local`). Keep this directory private. The CLI does not save the printed setup code or Wi-Fi password. Use a trusted LAN because the local API uses HTTP.
