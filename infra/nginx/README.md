# nginx

One site file, `finstone.conf`. `SERVER_NAME`, `API_PORT` and `UI_PORT` are
substituted by [../scripts/install.sh](../scripts/install.sh) on the way to
`/etc/nginx/sites-available/finstone`.

To do it by hand:

```bash
sed -e 's|SERVER_NAME|finstone.lan|' -e 's|API_PORT|8000|' -e 's|UI_PORT|8080|' \
    infra/nginx/finstone.conf | sudo tee /etc/nginx/sites-available/finstone
sudo ln -sfn /etc/nginx/sites-available/finstone /etc/nginx/sites-enabled/finstone
sudo nginx -t && sudo systemctl reload nginx
```

## One origin

The UI is served at `/` and the API at `/api/`, so a browser talks to a single
host and CORS never enters the picture. The alternative — two ports on the LAN —
means every client has to be told about both, and every self-hoster has to get a
CORS list right before anything loads.

The client still asks which server to talk to, as architecture §5.2 requires.
Behind this file the answer is just this host, with no port.

`proxy_pass` for `/api/` has **no trailing slash**, so the full path arrives
intact. With one, nginx would strip `/api/` and the versioned prefix the whole
contract is built on would never reach the application.

## Not this file's job

Cache policy. The UI container already sets it — hashed assets immutable,
`index.html` `no-store` — and two places that can disagree about which build a
returning browser gets is how a self-hoster ends up running two versions at
once.

## Before you use it

**There is no authentication behind this proxy.** Read
[../../docs/deploy.md](../../docs/deploy.md) first. TLS and a stopgap credential
are both covered there, along with what each one actually buys.
