#!/usr/bin/env bash
# Разворачивание прикладного сервера флота с нуля (docs/MIGRATION-3-SERVERS.md).
#
#   bootstrap-server.sh <A|B> <приватный_ключ_wireguard_файл>
#
# Идемпотентен: повторный запуск ничего не ломает. Поддерживает и Debian/Ubuntu, и
# RHEL-подобные (A и B оказались на разных дистрибутивах — Ubuntu 26.04 и CentOS Stream 9).
set -uo pipefail

SELF="${1:?первый аргумент: A или B}"
WGKEY="${2:?второй аргумент: файл с приватным ключом WireGuard для этого сервера}"
case "$SELF" in A) MYIP=10.10.0.1;; B) MYIP=10.10.0.2;; *) echo "только A или B"; exit 2;; esac
PEER_APP=$([ "$SELF" = "A" ] && echo B || echo A)
PEER_APP_IP=$([ "$SELF" = "A" ] && echo 10.10.0.2 || echo 10.10.0.1)
PEER_APP_EP=$([ "$SELF" = "A" ] && echo 65.21.144.39 || echo 87.239.135.218)

step() { echo; echo "== $* =="; }

step "1/7 Docker и утилиты"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq ca-certificates curl gnupg wireguard-tools jq git rsync >/dev/null
  if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    . /etc/os-release
    for c in "$VERSION_CODENAME" noble jammy; do
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $c stable" > /etc/apt/sources.list.d/docker.list
      apt-get update -qq 2>/dev/null && apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 && break
    done
    command -v docker >/dev/null || { rm -f /etc/apt/sources.list.d/docker.list; apt-get update -qq; apt-get install -y -qq docker.io docker-compose-v2 >/dev/null; }
  fi
else
  dnf -y -q install dnf-plugins-core wireguard-tools jq git rsync >/dev/null 2>&1
  command -v docker >/dev/null || {
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1 || \
    dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
    dnf -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1
  }
fi
systemctl enable --now docker >/dev/null 2>&1
docker --version

step "2/7 Туннель WireGuard ($MYIP)"
mkdir -p /etc/wireguard && chmod 700 /etc/wireguard
install -m 600 "$WGKEY" /etc/wireguard/privatekey
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = $MYIP/24
ListenPort = 51820
PostUp = wg set %i private-key /etc/wireguard/privatekey

[Peer]
# R — маршрутизатор и распорядитель ролей
PublicKey = Z+kuko2Tl9wKvWW28NhuHawRYL1v/8KITL01xajSZTc=
AllowedIPs = 10.10.0.3/32
Endpoint = 2.29.30.63:51820
PersistentKeepalive = 25

[Peer]
# $PEER_APP — второй прикладной сервер (репликация идёт по этому каналу)
PublicKey = __PEER_APP_KEY__
AllowedIPs = $PEER_APP_IP/32
Endpoint = $PEER_APP_EP:51820
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg0.conf
echo "ВНИМАНИЕ: подставьте публичный ключ второго прикладного сервера вместо __PEER_APP_KEY__"

step "3/7 Swap — страховка на случай, когда сервер примет весь флот"
if ! swapon --show | grep -q .; then
  fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
mkdir -p /etc/sysctl.d && echo "vm.swappiness=10" > /etc/sysctl.d/99-fleet.conf && sysctl -qp /etc/sysctl.d/99-fleet.conf 2>/dev/null
free -g | awk '/Swap:/{print "swap: " $2 " ГБ"}'

step "4/7 Репозиторий и каталоги инстансов"
docker network inspect web >/dev/null 2>&1 || docker network create web >/dev/null
mkdir -p /opt
if [ ! -d /opt/claude-ios/.git ]; then
  echo "нужен ключ доступа к репозиторию в /root/.ssh/github_deploy — создайте и добавьте его в GitHub"
  [ -f /root/.ssh/github_deploy ] || { echo "ключа нет, шаг пропущен"; }
fi
if [ -f /root/.ssh/github_deploy ] && [ ! -d /opt/claude-ios/.git ]; then
  grep -q "Host github.com" /root/.ssh/config 2>/dev/null || printf 'Host github.com\n  IdentityFile /root/.ssh/github_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking no\n' >> /root/.ssh/config
  git clone --quiet git@github.com:eliseiv/claude-ios.git /opt/claude-ios
fi
if [ -d /opt/claude-ios ]; then
  while read -r inst _ _; do
    case "$inst" in ""|\#*) continue;; esac
    [ -d "/opt/$inst" ] || cp -a /opt/claude-ios "/opt/$inst"
  done < /opt/claude-ios/infra/fleet/ports.txt
  echo "каталогов инстансов: $(ls -d /opt/*/ | wc -l)"
fi

step "5/7 Инструменты флота"
mkdir -p /opt/fleet
if [ -d /opt/claude-ios/infra/fleet ]; then
  cp /opt/claude-ios/infra/fleet/{provision.sh,replication.sh,import-from-old.sh,role-guard.sh,ports.txt} /opt/fleet/ 2>/dev/null
  chmod +x /opt/fleet/*.sh 2>/dev/null
  cp /opt/fleet/role-guard.sh /usr/local/bin/fleet-role-guard && chmod +x /usr/local/bin/fleet-role-guard
fi

step "6/7 Страж ролей"
echo "FLEET_SELF=$SELF" > /etc/fleet.conf
cat > /etc/systemd/system/fleet-role-guard.service <<'EOF'
[Unit]
Description=Страж ролей флота (приводит роли инстансов к таблице маршрутизатора)
After=docker.service network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/fleet.conf
ExecStart=/usr/local/bin/fleet-role-guard
EOF
cat > /etc/systemd/system/fleet-role-guard.timer <<'EOF'
[Unit]
Description=Периодическая сверка ролей с маршрутизатором

[Timer]
OnBootSec=15s
OnUnitActiveSec=5min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload && systemctl enable fleet-role-guard.timer >/dev/null 2>&1
echo "страж ролей установлен"

step "7/7 Запасной вход"
mkdir -p /opt/router/dynamic /opt/router/letsencrypt
if [ -d /opt/claude-ios/infra/fleet/router ]; then
  cp /opt/claude-ios/infra/fleet/router/traefik.yml /opt/router/traefik.yml
  cp /opt/claude-ios/infra/fleet/router/docker-compose.yml /opt/router/docker-compose.yml
  cp /opt/claude-ios/infra/fleet/dynamic.yml /opt/router/dynamic/fleet.yml
  echo "конфигурация запасного входа на месте (поднимать: cd /opt/router && docker compose up -d)"
fi

echo
echo "ОСТАЛОСЬ ВРУЧНУЮ:"
echo "  1. подставить публичный ключ второго прикладного сервера в /etc/wireguard/wg0.conf"
echo "  2. systemctl enable --now wg-quick@wg0 && ping -c2 10.10.0.3"
echo "  3. перенести инстансы: /opt/fleet/import-from-old.sh <инстанс> $SELF"
