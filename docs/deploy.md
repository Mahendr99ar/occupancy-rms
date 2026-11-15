# Running it on EC2

These steps assume the same box as the other portfolio projects: Ubuntu, Docker, and a Caddy container that already serves the other sites with HTTPS.

## 1. Check memory first

The stack adds MySQL, MongoDB, Redis and five Python containers. On a 4 GiB instance that is tight next to the other sites.

```bash
free -h
docker stats --no-stream
```

If less than about 1.5 GiB is free, add swap before starting:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Get the code and set secrets

```bash
cd ~ && git clone https://github.com/Mahendr99ar/occupancy-rms.git
cd occupancy-rms
cp .env.example .env
nano .env        # set MYSQL_PASSWORD, MYSQL_ROOT_PASSWORD and a long random API_TOKEN
```

`openssl rand -hex 24` gives a good token. Keep `.env` on the server only.

## 3. Start the stack

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f cycle      # one JSON line per cycle
```

The first build downloads CPU-only PyTorch and takes several minutes. The `cycle` service crawls every 15 minutes. To make the simulated market move one day per cycle (for a demo), change its command to `["python", "-m", "occupancy.cycle", "--advance", "--every", "900"]`.

The API and the mock market listen on 127.0.0.1 only.

## 4. Put it behind Caddy

Point a DuckDNS name (for example `occupancy-mahendra.duckdns.org`) at the instance's public IP.

Find the network Caddy is on and connect the API container to it:

```bash
docker inspect caddy --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker network connect <that-network> occupancy-rms-api-1
```

Add a site block to `/home/ubuntu/Caddyfile`:

```
occupancy-mahendra.duckdns.org {
    reverse_proxy occupancy-rms-api-1:8000
}
```

Reload Caddy:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
curl -sI https://occupancy-mahendra.duckdns.org | head -1
```

The `docker network connect` step has to be repeated if the API container is recreated. To make it permanent, add Caddy's network to the `api` service in `docker-compose.yml` as an external network.

## 5. Approving rates from the dashboard

The dashboard asks for the API token the first time a rate is approved or rejected, and keeps it in the browser's session storage. Anyone without the token can view the dashboard but cannot push rates.
