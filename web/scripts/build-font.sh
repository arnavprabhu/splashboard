#!/usr/bin/env bash
# Rebuilds src/assets/fonts/archivo-latin.woff2 from the google/fonts variable TTF.
# Keeps both axes, limited to the ranges the design uses (wght 400-900, wdth 62-100%),
# and subsets to Latin. Budget: <= 90 KB.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
base='https://github.com/google/fonts/raw/main/ofl/archivo'
curl -sSL -o "$work/Archivo.ttf" "$base/Archivo%5Bwdth,wght%5D.ttf"
curl -sSL -o "$here/src/assets/fonts/OFL.txt" "$base/OFL.txt"
cp "$here/src/assets/fonts/OFL.txt" "$here/public/licenses/Archivo-OFL.txt"
uvx --from 'fonttools[woff]' fonttools varLib.instancer "$work/Archivo.ttf" \
  wght=400:900 wdth=62:100 -o "$work/Archivo-limited.ttf"
uvx --from 'fonttools[woff]' pyftsubset "$work/Archivo-limited.ttf" \
  --output-file="$here/src/assets/fonts/archivo-latin.woff2" --flavor=woff2 \
  --unicodes='U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2199,U+2212,U+2215,U+2248,U+2260,U+2264,U+2265,U+FEFF,U+FFFD' \
  --layout-features='kern,liga,calt,tnum,lnum,case,ccmp,locl,mark,mkmk' \
  --no-hinting --desubroutinize --drop-tables+=DSIG
ls -l "$here/src/assets/fonts/archivo-latin.woff2"
