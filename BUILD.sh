#!/bin/sh
# Build the single-file executable. Needs: pip install pyinstaller flask
#
# templates/, static/ and reports/ are bundled, so the captured report bodies
# travel with the binary. data/ is not: it is written next to it at runtime.
set -e

pyinstaller --onefile --name LabKPIs \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --add-data "reports:reports" \
  app.py

echo
echo "Built dist/LabKPIs"
