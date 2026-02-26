# Read Next — Deploy Notes

## Local dev

```
python3 -m http.server 8000
```

Open http://localhost:8000

## Production (exe.dev)

VM: `readnext`
URL: https://readnext.exe.xyz

### Deploy

Copy files to the VM and into nginx's web root:

```
scp index.html links.txt readnext.exe.xyz:
ssh readnext.exe.xyz 'sudo cp ~/index.html ~/links.txt /var/www/html/'
```

### First-time setup

The VM comes with nginx pre-installed but not enabled. Start it with:

```
ssh readnext.exe.xyz sudo systemctl enable --now nginx
```

exe.dev proxies `https://readnext.exe.xyz` to port 80 on the VM, where nginx serves `/var/www/html/`.

### SSH access

```
ssh readnext.exe.xyz
```
