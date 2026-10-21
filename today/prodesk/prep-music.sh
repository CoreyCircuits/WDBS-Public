#!/usr/bin/env bash
# Get tracks ready for the WDBS Today music bed.
# Two-pass loudness normalization to the WDBS music-bed level (-18 LUFS integrated,
# -1.5 dBTP, same as the WeatherCast bed), then Opus in an .ogg file, which FS42's Qt
# browser always plays (it may not play MP3 or AAC).
#
#   prep-music.sh [inbox] [music folder]
#   defaults: ~/wdbs-today/music-inbox  ->  ~/wdbs-today/www/music
#
# Drop files in the inbox, run this, and the next feed run (within 5 minutes) puts
# them in rotation. Originals are left alone; tracks already converted are skipped.
set -euo pipefail
IN="${1:-$HOME/wdbs-today/music-inbox}"
OUT="${2:-$HOME/wdbs-today/www/music}"
TARGET="I=-18:TP=-1.5:LRA=11"
mkdir -p "$IN" "$OUT"
command -v ffmpeg >/dev/null || { echo "ffmpeg is not installed. On Fedora: sudo dnf install ffmpeg-free" >&2; exit 1; }

shopt -s nullglob nocaseglob
files=("$IN"/*.{mp3,m4a,aac,wav,flac,ogg,opus,webm})
[[ ${#files[@]} -eq 0 ]] && { echo "No audio files in $IN"; exit 0; }

for f in "${files[@]}"; do
  name="$(basename "${f%.*}")"
  dest="$OUT/$name.ogg"
  if [[ -e "$dest" ]]; then echo "skip  $name (already converted)"; continue; fi
  stats=$(ffmpeg -hide_banner -nostats -i "$f" -vn -af "loudnorm=$TARGET:print_format=json" -f null - 2>&1 | sed -n '/^{/,/^}/p')
  read -r mi mtp mlra mth off < <(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d["input_i"], d["input_tp"], d["input_lra"], d["input_thresh"], d["target_offset"])' <<<"$stats")
  ffmpeg -hide_banner -loglevel error -y -i "$f" -vn -map_metadata -1 \
    -af "loudnorm=$TARGET:measured_I=$mi:measured_TP=$mtp:measured_LRA=$mlra:measured_thresh=$mth:offset=$off:linear=true,aresample=48000" \
    -c:a libopus -b:a 128k -f ogg "$OUT/.$name.part"
  mv "$OUT/.$name.part" "$dest"
  echo "done  $name  (was $mi LUFS)"
done
