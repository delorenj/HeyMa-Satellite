#!/bin/bash
# Record "Hey Tonny" wake word samples
# Run this script and follow the prompts

SAMPLES_DIR="/home/delorenj/hey_tonny_training/samples"
DEVICE="plughw:wm8960soundcard"

echo "=== Hey Tonny Wake Word Sample Recorder ==="
echo ""
read -p "Enter speaker name (e.g., mom, dad, kid1): " SPEAKER

mkdir -p "$SAMPLES_DIR/$SPEAKER"

COUNT=$(ls "$SAMPLES_DIR/$SPEAKER"/*.wav 2>/dev/null | wc -l)

echo ""
echo "Recording samples for: $SPEAKER"
echo "Existing samples: $COUNT"
echo ""
echo "Tips for good recordings:"
echo "  - Speak naturally, like you would to a voice assistant"
echo "  - Vary your distance (close, medium, across room)"
echo "  - Vary your tone (normal, tired, excited, whisper)"
echo "  - Background noise is OK - it helps training"
echo ""
echo "Press ENTER to record, 'q' to quit"
echo ""

while true; do
    read -p "Ready? [ENTER to record, q to quit]: " INPUT

    if [ "$INPUT" = "q" ]; then
        echo "Done! Recorded samples are in: $SAMPLES_DIR/$SPEAKER/"
        COUNT=$(ls "$SAMPLES_DIR/$SPEAKER"/*.wav 2>/dev/null | wc -l)
        echo "Total samples for $SPEAKER: $COUNT"
        exit 0
    fi

    COUNT=$(ls "$SAMPLES_DIR/$SPEAKER"/*.wav 2>/dev/null | wc -l)
    NEXT=$((COUNT + 1))
    FILENAME="$SAMPLES_DIR/$SPEAKER/hey_tonny_${SPEAKER}_$(printf "%03d" $NEXT).wav"

    echo "Recording in 1 second... Say 'Hey Tonny'"
    sleep 1

    # Record 2 seconds of audio
    arecord -D "$DEVICE" -f cd -t wav -d 2 "$FILENAME" 2>/dev/null

    echo "Saved: $FILENAME"
    echo ""
done
