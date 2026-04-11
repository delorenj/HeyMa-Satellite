# ReSpeaker 2-Mic HAT v2.0 Hardware Failure Analysis

**Date:** 2026-01-19
**Device:** Raspberry Pi Zero 2 W with ReSpeaker 2-Mic HAT v2.0
**Kernel:** 6.12.62+rpt-rpi-v8 (Debian Trixie)

## Summary

The ReSpeaker 2-Mic HAT v2.0 exhibits a hardware failure preventing all audio recording and playback. The TLV320AIC3104 audio codec chip shows intermittent I2C communication and complete I2S (audio streaming) failure.

## Symptoms

- `arecord` fails with "read error: Input/output error"
- `speaker-test` fails with "Write error: -5, Input/output error"
- Kernel logs show repeated "I2S SYNC error!" messages
- I2C register access fails with errors -5 (EIO) and -121 (EREMOTEIO)

## Diagnostic Findings

### 1. Device Detection

The codec is detected on the I2C bus at address 0x18:

```
$ sudo i2cdetect -y 1
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
10: -- -- -- -- -- -- -- -- UU -- 1a -- -- -- -- --
```

- **0x18**: TLV320AIC3104 codec (UU = bound to driver)
- **0x1a**: APA102 LED controller (functional)

### 2. ALSA Detection

The sound card appears in ALSA:

```
$ arecord -l
card 1: seeed2micvoicec [seeed2micvoicec], device 0: bcm2835-i2s-tlv320aic3x-hifi
```

### 3. Mixer Controls

ALSA mixer commands work intermittently, indicating partial I2C functionality:

```
$ amixer -c 1 sset 'Left PGA Mixer Line1L' on
Simple mixer control 'Left PGA Mixer Line1L',0
  Mono: Playback [on]
```

### 4. Critical I2C Anomaly

When the kernel driver is unbound, the codec **disappears from the I2C bus entirely**:

```
$ echo "1-0018" | sudo tee /sys/bus/i2c/drivers/tlv320aic3x/unbind
$ sudo i2cdetect -y 1
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
10: -- -- -- -- -- -- -- -- -- -- 1a -- -- -- -- --
```

The codec at 0x18 vanishes. This is abnormal - I2C devices should always respond to bus scans regardless of driver state.

### 5. Boot-Time Errors

```
tlv320aic3x 1-0018: supply IOVDD not found, using dummy regulator
tlv320aic3x 1-0018: supply DVDD not found, using dummy regulator
tlv320aic3x 1-0018: supply AVDD not found, using dummy regulator
tlv320aic3x 1-0018: Invalid supply voltage(s) AVDD: -22, DVDD: -22
```

### 6. Runtime Errors (during audio operations)

```
bcm2835-i2s 3f203000.i2s: I2S SYNC error!
tlv320aic3x 1-0018: Unable to sync registers 0x2-0x3. -5
tlv320aic3x 1-0018: ASoC: error at soc_component_write_no_lock for register: [0x00000005] -5
tlv320aic3x 1-0018: ASoC: error at snd_soc_component_update_bits for register: [0x00000013] -121
```

## Root Cause Analysis

The **I2S SYNC error** is the key indicator. The TLV320AIC3104 is configured as the I2S clock master (generates BCLK and LRCLK), but requires an external MCLK (master clock) input to function.

The ReSpeaker 2-Mic HAT v2.0 includes a **24.576 MHz crystal oscillator (Y1)** that provides MCLK to the codec. The failure pattern indicates this oscillator is not functioning:

1. Without MCLK, the codec cannot generate I2S clocks
2. The I2S interface fails to synchronize
3. The codec enters an unstable state, causing intermittent I2C failures
4. Register writes fail, audio streaming is impossible

## Probable Hardware Failures

In order of likelihood:

1. **Failed crystal oscillator (Y1)** - The 24.576 MHz oscillator has failed or has a cold solder joint
2. **Damaged TLV320AIC3104 codec** - The chip itself may be defective
3. **Poor GPIO header connection** - Intermittent contact on power (3.3V) or I2S signal pins
4. **PCB trace damage** - Break in MCLK or I2S signal traces

## Troubleshooting Attempted

| Action | Result |
|--------|--------|
| Power cycle | No improvement |
| Reseat HAT on GPIO header | No improvement |
| Reload kernel modules | No improvement |
| Enable mixer controls | Controls respond, audio still fails |
| Test with plughw device | Same I/O errors |
| Direct I2C register read | Fails when driver unbound |

## Recommendations

### Immediate Actions

1. **Visual inspection** - Examine the board for:
   - Cold solder joints on Y1 (crystal oscillator) and U1 (codec)
   - Physical damage to components or traces
   - Corrosion or contamination

2. **Multimeter test** - Verify 3.3V power at the codec:
   - Measure between GPIO pin 1 (3.3V) and pin 6 (GND)
   - Should read approximately 3.3V

3. **Test on another Pi** - Rule out Pi-specific issues by testing the HAT on a different Raspberry Pi

### Resolution Options

1. **RMA/Warranty replacement** - Contact Seeed Studio if the board is under warranty
   - Seeed Studio support: https://www.seeedstudio.com/contacts

2. **Board repair** (requires soldering skills):
   - Reflow solder on Y1 (24.576 MHz crystal oscillator)
   - Reflow solder on U1 (TLV320AIC3104 codec)
   - Check continuity of MCLK trace from Y1 to codec

3. **Purchase replacement**:
   - ReSpeaker 2-Mics Pi HAT v2.0: ~$15-20 USD
   - Alternative: ReSpeaker 4-Mic Array for better audio quality

## Alternative Wyoming Satellite Setup

If replacing the ReSpeaker HAT, consider these alternatives:

| Option | Pros | Cons |
|--------|------|------|
| ReSpeaker 2-Mic v2.0 (new) | Drop-in replacement, same config | Same potential failure mode |
| ReSpeaker 4-Mic Array | Better audio, AEC support | Higher cost, different overlay |
| USB microphone | No HAT required, simpler setup | No GPIO LEDs, separate speaker needed |
| I2S MEMS microphone | Compact, reliable | Requires custom wiring |

## Device Tree Overlay Reference

Current overlay in `/boot/firmware/config.txt`:

```
dtoverlay=respeaker-2mic-v2_0
```

The overlay correctly configures:
- I2C address 0x18 for TLV320AIC3104
- 24.576 MHz fixed clock reference
- Codec as I2S clock master
- simple-audio-card with proper routing

The overlay itself is not the cause of the failure.

## Files and Logs

Relevant kernel messages can be retrieved with:

```bash
dmesg | grep -i "tlv320\|i2s\|seeed"
```

ALSA configuration:

```bash
arecord -l
amixer -c 1 contents
```

---

**Conclusion:** The ReSpeaker 2-Mic HAT v2.0 has a hardware failure, most likely a failed 24.576 MHz crystal oscillator. The board should be replaced or repaired.
