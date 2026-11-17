#!/usr/bin/env python3
"""Scoped NL71K2 stored-static scene test, using the existing secure client.

Provenance: official Nanoleaf Desktop 3.0.2 (installer SHA256
fc8512c9d2c9e21171f4fa1f851b5cad2b50e20da4f5de2f631bc00579727f65),
main.js functions kXt (layout), lne/wJt (static format), Q$r (readback).
No vendor code is executed. No credentials are exported. No scene deletion,
overwriting, firmware changes, or unattended retry of mutations is provided.
"""
import colorsys
import datetime
import struct

from . import native as nl


def tlvs(data):
    out = []
    while data:
        if len(data) < 3:
            raise ValueError('Truncated TLV header')
        tag, size = struct.unpack('>BH', data[:3])
        if len(data) < size + 3:
            raise ValueError('Truncated TLV value')
        out.append((tag, data[3:3+size]))
        data = data[3+size:]
    return out


def ids(s):
    raw = dict(tlvs(s.request('effect/ids')))[8]
    if len(raw) % 4:
        raise ValueError('Invalid effect IDs')
    return [x[0] for x in struct.iter_unpack('>I', raw)]


def read_effect(s, ident):
    reply = s.request('effect', nl.tlv(8, struct.pack('>I', ident)))
    outer = tlvs(reply)
    if len(outer) != 1 or outer[0][0] != 1:
        raise ValueError('Invalid effect list')
    entries = tlvs(outer[0][1])
    if len(entries) != 1 or entries[0][0] != 2:
        raise ValueError('Expected exactly one effect')
    fields = dict(tlvs(entries[0][1]))
    if int.from_bytes(fields[3], 'big') != ident:
        raise ValueError('Wrong effect ID returned')
    return entries[0][1]



def layout(s):
    count = s.request('lb/0/lsLength')
    zones = s.request('lb/0/lpz')
    if len(count) != 2 or len(zones) != 4:
        raise ValueError('Unexpected layout response')
    leds = int.from_bytes(count, 'big')
    per_zone, secondary = struct.unpack('>HH', zones)
    # kXt uses the first word for paint grouping. Its min/max labels conflict
    # with this real firmware's 0002 000f response; don't infer a minimum from
    # that second word or silently choose a finer grouping. Retain raw evidence.
    if not 1 <= per_zone <= 255 or not 1 <= leds <= 4096:
        raise ValueError('Unexpected layout bounds')
    return {'led_count': leds, 'leds_per_zone': per_zone,
            'lpz_secondary_word': secondary, 'lpz_raw_hex': zones.hex(),
            'zone_count': (leds + per_zone - 1)//per_zone}


def snapshot(s):
    return {'created': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'model': nl.MODEL, 'layout': layout(s),
            'selected': s.request('lb/0/effect/select').decode().rstrip('\0'),
            'power_loss_recovery_hex': s.request('lb/0/plr').hex(),
            'effects': {str(i): read_effect(s, i).hex() for i in ids(s)}}


def encode_static(config, ident, shape):
    # Match the device-reported grouping; do not invent LED addresses.
    colors = [bytes.fromhex(c['hex']) for c in config['colors']]
    if not colors or len(colors) > 10 or any(len(c) != 3 or 255 in c for c in colors):
        raise ValueError('Need 1..10 RGB colors; 255 is a static-data skip sentinel')
    pixels = b''.join(colors[i % len(colors)] for i in range(shape['zone_count']))
    hsv = bytearray()
    for color in colors:
        h, sat, val = colorsys.rgb_to_hsv(*(v/255 for v in color))
        hsv.extend(struct.pack('>HBB', int(h*360+.5), int(sat*100+.5), int(val*100+.5)))
    return (nl.tlv(3, struct.pack('>I', ident))
            + nl.tlv(4, bytes.fromhex('ff030000'))
            + nl.tlv(5, config['name'].encode())
            + nl.tlv(6, nl.tlv(1, b'\0') + nl.tlv(2, bytes(hsv)))
            + nl.tlv(7, b'')
            + nl.tlv(9, nl.tlv(1, b'\1') + nl.tlv(2, pixels)
                     + nl.tlv(3, bytes([shape['leds_per_zone']])))
            + nl.tlv(11, b'\xff'))


def verify_static(actual, config, ident, shape):
    expected = dict(tlvs(encode_static(config, ident, shape)))
    got = dict(tlvs(actual))
    for tag in (3, 4, 5, 6, 9, 11):
        if got.get(tag) != expected[tag]:
            raise ValueError(f'Stored scene differs at field {tag}')


def store_static(s, config, before, allow_existing=False):
    """Add one verified static scene, preserving every existing scene byte."""
    same = [int(i) for i, raw in before['effects'].items()
            if dict(tlvs(bytes.fromhex(raw)))[5].decode() == config['name']]
    if same:
        if len(same) != 1 or not allow_existing:
            raise ValueError('Scene name exists; refusing to overwrite it')
        ident = same[0]
        verify_static(bytes.fromhex(before['effects'][str(ident)]), config, ident, before['layout'])
        return ident
    old_ids = set(map(int, before['effects']))
    if len(old_ids) >= 32:
        raise ValueError('At Desktop firmware-5 scene limit; no scene removed')
    ident = next(i for i in range(1, 255) if i not in old_ids)
    encoded = encode_static(config, ident, before['layout'])
    s.request('effect', nl.tlv(1, nl.tlv(2, encoded)), 1)
    verify_static(read_effect(s, ident), config, ident, before['layout'])
    if set(ids(s)) != old_ids | {ident}:
        raise ValueError('Unexpected stored scene list after save')
    for old_id, raw in before['effects'].items():
        if read_effect(s, int(old_id)).hex() != raw:
            raise ValueError(f'Existing effect {old_id} changed')
    return ident
