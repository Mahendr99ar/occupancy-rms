#!/usr/bin/env bash
# Retrains the three models the repo ships in models/. CPU only.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m occupancy.ml.price_reader          # a few minutes
[ -f data/hotel_bookings.csv ] || scripts/get_data.sh
python -m occupancy.ml.cancellation          # about 7 minutes
python -m occupancy.ml.slm train --steps 5000 # about an hour on 2 cores
python -m occupancy.ml.slm eval
