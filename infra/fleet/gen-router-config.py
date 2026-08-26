#!/usr/bin/env python3
"""Генератор динамической конфигурации Traefik для маршрутизатора (docs/MIGRATION-3-SERVERS.md).

Источник истины — `infra/fleet/instances.tsv`. Конфигурация НЕ пишется руками: она
разворачивается на трёх узлах (рабочий вход R и запасные на A и B), и расхождение между
ними означает, что запасной вход не выручит ровно в тот момент, когда понадобится.

Отказоустойчивость выражена типом сервиса `failover`: трафик идёт на основной сервер, а на
резервный переключается ТОЛЬКО когда проверка здоровья основного перестала проходить.
Обычная балансировка здесь недопустима — у резервного инстанса база работает на чтение.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

WG = {"A": "10.10.0.1", "B": "10.10.0.2"}
HERE = pathlib.Path(__file__).resolve().parent
UNRESOLVED = "ПОДЛЕЖИТ_УТОЧНЕНИЮ"


def read_rows():
    rows = []
    for line in (HERE / "instances.tsv").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, domain, port, primary = line.split("\t")
        rows.append((name, domain, int(port), primary))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tls",
        action="store_true",
        help=(
            "выпускать сертификаты Let's Encrypt. ВКЛЮЧАТЬ ТОЛЬКО ПОСЛЕ переключения A-записей: "
            "до него проверка ACME идёт на старый адрес, каждая попытка проваливается и "
            "расходует часовой лимит неудачных проверок."
        ),
    )
    args = ap.parse_args()
    rows = read_rows()
    skipped = [n for n, d, _, _ in rows if d == UNRESOLVED]
    live = [r for r in rows if r[1] != UNRESOLVED]

    out = ["# СГЕНЕРИРОВАНО gen-router-config.py — не править руками.", "http:", "  routers:"]
    for name, domain, _port, _primary in live:
        out += [
            f"    {name}:",
            f"      rule: \"Host(`{domain}`)\"",
            f"      entryPoints: [{'websecure' if args.tls else 'web'}]",
            f"      service: {name}",
        ]
        if args.tls:
            out += ["      tls:", "        certResolver: le"]
    out.append("  services:")
    for name, _domain, port, primary in live:
        standby = "B" if primary == "A" else "A"
        out += [
            f"    {name}:",
            "      failover:",
            f"        service: {name}-primary",
            f"        fallback: {name}-standby",
            "        healthCheck: {}",
        ]
        for role, srv in (("primary", primary), ("standby", standby)):
            out += [
                f"    {name}-{role}:",
                "      loadBalancer:",
                "        servers:",
                f"          - url: \"http://{WG[srv]}:{port}\"",
                "        healthCheck:",
                "          path: /ready",
                "          interval: 10s",
                "          timeout: 3s",
            ]

    dest = HERE / "dynamic.yml"
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"инстансов в конфиге: {len(live)}  TLS: {'да' if args.tls else 'нет (до переключения DNS)'}")
    if skipped:
        print(f"ПРОПУЩЕНО (домен не подтверждён): {', '.join(skipped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
