#!/usr/bin/env bash
# One-time email setup for ki-ops notify-perturbs (Google Workspace SMTP).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLE="$ROOT/config/notify.env.example"
TARGET="$ROOT/config/notify.env"

if [[ ! -f "$TARGET" ]]; then
  cp "$EXAMPLE" "$TARGET"
  echo "Created $TARGET"
else
  echo "Already exists: $TARGET"
fi

echo ""
echo "Next steps:"
echo "  1. Google Account (robert@kelaitech.com) → Security → App passwords"
echo "  2. Create app password for Mail / ki-ops"
echo "  3. Edit config/notify.env → set KI_OPS_SMTP_PASSWORD=..."
echo "  4. cd $ROOT && .venv/bin/ki-ops extras notify-config"
echo "  5. .venv/bin/ki-ops extras notify-perturbs --test-email --skip-slack"
echo ""
