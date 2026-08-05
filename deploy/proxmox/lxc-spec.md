# Proxmox LXC specification

- Unprivileged Debian 13 container; use Debian 12 only if the host has no compatible Debian 13 template.
- 2 CPU cores, 4096 MiB RAM, 512 MiB swap, 40 GiB root disk.
- Enable start at boot and set startup order after the NPM guest.
- Use `vmbr0`, a fixed generated MAC address, and DHCP during creation.
- Reserve the resulting address for that MAC in the router before configuring NPM.
- Do not enable nesting, keyctl, FUSE, or Docker-related features.
- Enable the Proxmox firewall. Permit TCP 22 only from the admin LAN and TCP 8080 only from the NPM guest. Permit outbound DNS, NTP, HTTPS, and PostgreSQL package mirrors during installation.
- Add the administrator's SSH public key during container creation; disable SSH password login after validation.
