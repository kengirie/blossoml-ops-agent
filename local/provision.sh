#!/bin/bash
# Image build time: blossoml_ops/deploy/README.md, steps that do not need a
# running system. Steps that do (secrets, firewall, seed data) are in lab-init.sh.
set -euxo pipefail
D=/opt/lab/deploy

# --- 1. blossoML (design 8): drop-in, working directory, binary ---------------
install -D -m 0644 $D/systemd/blossoml.service.d/override.conf /etc/systemd/system/blossoml.service.d/override.conf
install -d -o ubuntu -g ubuntu -m 0750 /home/ubuntu/blossoML /home/ubuntu/blossoML/data /home/ubuntu/blossoML/db
chown ubuntu:ubuntu /home/ubuntu/blossoML/main.exe
chmod 0755 /home/ubuntu/blossoML/main.exe
cp -p /home/ubuntu/blossoML/main.exe /home/ubuntu/blossoML/main.exe.backup

# --- 2. user and permissions (7.1) ---------------------------------------------
useradd --system --home-dir /var/lib/blossoml-ops --create-home --shell /usr/sbin/nologin blossoml-ops
usermod -aG systemd-journal blossoml-ops
install -d -o blossoml-ops -g blossoml-ops -m 0750 /var/lib/blossoml-ops /var/lib/blossoml-ops/work
setfacl -m u:blossoml-ops:x  /home/ubuntu /home/ubuntu/blossoML
setfacl -m u:blossoml-ops:rx /home/ubuntu/blossoML/data /home/ubuntu/blossoML/db
setfacl -m u:blossoml-ops:r  /home/ubuntu/blossoML/main.exe /home/ubuntu/blossoML/main.exe.backup
# (blossom.db does not exist yet; lab-init.sh adds its ACL.)

# --- 3. egress (7.3) -----------------------------------------------------------
install -m 0644 $D/proxy/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
install -m 0644 $D/proxy/blossoml-ops.filter /etc/tinyproxy/blossoml-ops.filter
install -D -m 0644 $D/nftables/blossoml-ops.nft /etc/nftables.d/blossoml-ops.nft

# --- 4. config and systemd (7.4) -----------------------------------------------
install -d -o root -g blossoml-ops -m 0750 /etc/blossoml-ops
install -m 0644 $D/config.example.yaml /etc/blossoml-ops/config.yaml
install -m 0644 $D/systemd/blossoml-ops@.service /etc/systemd/system/
install -m 0644 $D/systemd/blossoml-ops-daily.timer /etc/systemd/system/
# The daily timer is not enabled in the lab: runs are started by hand.

# --- 5. application layout and install (7.7) -----------------------------------
install -d -o ubuntu -g ubuntu -m 0755 /opt/blossoml-ops /opt/blossoml-ops/releases /opt/blossoml-ops/incoming /opt/blossoml-ops/bin
install -o root -g root -m 0755 $D/bin/run-manual /opt/blossoml-ops/bin/run-manual
install -o ubuntu -g ubuntu -m 0644 /opt/lab/blossoml-ops-lab.tar $D/remote-install.sh /opt/blossoml-ops/incoming/
# Exactly what CI runs over ssh, as the deploy user.
sudo -u ubuntu bash /opt/blossoml-ops/incoming/remote-install.sh /opt/blossoml-ops/incoming/blossoml-ops-lab.tar
rm -f /opt/lab/blossoml-ops-lab.tar /opt/blossoml-ops/incoming/remote-install.sh

# --- 10. phase 2 files (installed, but phase2.enabled stays false) --------------
install -d /usr/local/libexec/blossoml-ops
install -o root -g root -m 0755 $D/libexec/backup-db $D/libexec/cleanup-tmp /usr/local/libexec/blossoml-ops/
install -m 0644 $D/systemd/blossoml-ops-backup-db.service "$D/systemd/blossoml-ops-cleanup-tmp@.service" /etc/systemd/system/
visudo -cf $D/sudoers/blossoml-ops
install -o root -g root -m 0440 $D/sudoers/blossoml-ops /etc/sudoers.d/blossoml-ops

systemctl enable blossoml.service tinyproxy.service lab-init.service
