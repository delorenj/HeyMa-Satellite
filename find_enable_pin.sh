#!/bin/bash
# Try chip names and numbers
for chip in gpiochip4 gpiochip0 4 0; do
  for pin in 12 25; do
    echo "Trying chip '$chip' pin '$pin'..."
    
    # Try to set the pin. 
    gpioset "$chip" "$pin=1" &
    PID=$!
    sleep 1
    
    # Check if gpioset is still running
    if ! kill -0 $PID 2>/dev/null; then
       echo "  -> gpioset failed/exited immediately"
       continue
    fi
    
    echo "  -> gpioset running. Testing audio..."
    timeout 3s speaker-test -D hw:1,0 -c 2 -t sine -f 440 > /tmp/audio_test_log 2>&1
    
    if grep -q "Input/output error" /tmp/audio_test_log; then
       echo "  -> Failed (I/O Error)"
    elif grep -q "write error" /tmp/audio_test_log; then
       echo "  -> Failed (Write Error)"
    else
       echo "SUCCESS! Audio played without I/O error on chip '$chip' pin '$pin'"
       # Keep it running or kill it? 
       # The user wants confirmation. We found the fix.
       kill $PID
       exit 0
    fi
    
    kill $PID 2>/dev/null
  done
done
echo "All combinations failed."
exit 1
