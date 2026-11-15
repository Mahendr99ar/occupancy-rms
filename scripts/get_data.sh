#!/usr/bin/env bash
# Downloads the public Hotel Booking Demand data (Antonio, Almeida and Nunes,
# Data in Brief, 2019) used to train the cancellation model, and checks it.
set -euo pipefail
cd "$(dirname "$0")/.."
URL=https://raw.githubusercontent.com/rfordatascience/tidytuesday/master/data/2020/2020-02-11/hotels.csv
SHA=7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06
mkdir -p data
curl -fsSL "$URL" -o data/hotel_bookings.csv
echo "$SHA  data/hotel_bookings.csv" | sha256sum -c -
