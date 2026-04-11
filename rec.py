import sounddevice as sd
import numpy as np
import wave

# List all available audio devices
print("Available audio devices:")
devices = sd.query_devices()
for i, device in enumerate(devices):
    print(f"Device {i}: {device['name']} (input: {device['max_input_channels']}, output: {device['max_output_channels']})")

# Find the ReSpeaker device
respeaker_device = None
for i, device in enumerate(devices):
    if 'seeed' in device['name'].lower() or 'bcm2835' in device['name'].lower():
        respeaker_device = i
        print(f"\nFound ReSpeaker device: {i} - {device['name']}")
        break

if respeaker_device is None:
    print("\nReSpeaker device not found explicitly, using default input device")
    respeaker_device = None

# Record 5 seconds of audio
duration = 5  # seconds
sample_rate = 16000
channels = 1  # Mono recording

print(f"\nRecording {duration} seconds of audio...")
try:
    if respeaker_device is not None:
        recording = sd.rec(int(duration * sample_rate),
                          samplerate=sample_rate,
                          channels=channels,
                          dtype='int16',
                          device=respeaker_device)
    else:
        recording = sd.rec(int(duration * sample_rate),
                          samplerate=sample_rate,
                          channels=channels,
                          dtype='int16')

    sd.wait()

    # Save as WAV file
    with wave.open('test_recording.wav', 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(recording.tobytes())

    print("Recording saved to test_recording.wav")
    print(f"File size: {len(recording) * 2} bytes")

except Exception as e:
    print(f"Error during recording: {e}")

