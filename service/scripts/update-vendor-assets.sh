#!/usr/bin/env bash
# update-vendor-assets.sh
# Downloads Bootstrap and Google Fonts for local self-hosting.
# Re-run this script to update versions — change the variables below.
set -euo pipefail

# --------------- Configuration ---------------
BOOTSTRAP_VERSION="5.3.3"

# Google Fonts URL — same as the one currently in base.html.
# Requesting with a Chrome user-agent ensures Google serves woff2 format.
GOOGLE_FONTS_URL="https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400&family=Outfit:wght@300;400;500;600;700&display=swap"

CHROME_UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# --------------- Paths ---------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="${SCRIPT_DIR}/../static/assets/vendor"
BOOTSTRAP_DIR="${VENDOR_DIR}/bootstrap-${BOOTSTRAP_VERSION}"
FONTS_DIR="${VENDOR_DIR}/fonts"

# --------------- Bootstrap ---------------
echo "==> Downloading Bootstrap ${BOOTSTRAP_VERSION}..."

mkdir -p "${BOOTSTRAP_DIR}/css" "${BOOTSTRAP_DIR}/js"

curl -fsSL "https://cdn.jsdelivr.net/npm/bootstrap@${BOOTSTRAP_VERSION}/dist/css/bootstrap.min.css" \
    -o "${BOOTSTRAP_DIR}/css/bootstrap.min.css"
echo "    bootstrap.min.css"

curl -fsSL "https://cdn.jsdelivr.net/npm/bootstrap@${BOOTSTRAP_VERSION}/dist/js/bootstrap.bundle.min.js" \
    -o "${BOOTSTRAP_DIR}/js/bootstrap.bundle.min.js"
echo "    bootstrap.bundle.min.js"

# --------------- Google Fonts ---------------
echo "==> Downloading Google Fonts..."

# Fetch the CSS that Google generates (contains @font-face with woff2 URLs)
TMPFILE=$(mktemp)
trap 'rm -f "${TMPFILE}"' EXIT
curl -fsSL -H "User-Agent: ${CHROME_UA}" "${GOOGLE_FONTS_URL}" -o "${TMPFILE}"

# Parse font metadata and download each woff2 file.
# Google's CSS has comments like /* latin */ and @font-face blocks.
# We extract: font-family, font-style, font-weight, unicode-range, and the woff2 src url.
# Note: in Google's CSS, unicode-range comes AFTER the src line,
# so we collect all fields and emit the @font-face on the closing brace.
FONTS_CSS=""
woff2_regex='url\((https://[^)]+\.woff2)\)'

# Per-block accumulators (reset on each subset comment)
current_subset=""
current_family=""
current_style=""
current_weight=""
current_range=""
current_woff2_url=""

while IFS= read -r line; do
    # Track subset comments (e.g., /* latin */)
    if [[ "$line" =~ ^/\*\ (.+)\ \*/ ]]; then
        current_subset="${BASH_REMATCH[1]}"
        continue
    fi

    # Extract font-family
    if [[ "$line" =~ font-family:\ \'([^\']+)\' ]]; then
        current_family="${BASH_REMATCH[1]}"
    fi

    # Extract font-style
    if [[ "$line" =~ font-style:\ ([^;]+)\; ]]; then
        current_style="${BASH_REMATCH[1]}"
    fi

    # Extract font-weight
    if [[ "$line" =~ font-weight:\ ([^;]+)\; ]]; then
        current_weight="${BASH_REMATCH[1]}"
    fi

    # Extract woff2 URL (but don't download yet — need unicode-range first)
    if [[ "$line" =~ $woff2_regex ]]; then
        current_woff2_url="${BASH_REMATCH[1]}"
    fi

    # Extract unicode-range
    if [[ "$line" =~ unicode-range:\ ([^;]+)\; ]]; then
        current_range="${BASH_REMATCH[1]}"
    fi

    # On closing brace, download and emit if we collected a woff2 URL
    if [[ "$line" == "}" ]] && [[ -n "${current_woff2_url}" ]]; then
        # Guard against incomplete font-face data (e.g., if Google changes CSS format)
        if [[ -z "${current_family}" || -z "${current_style}" || -z "${current_weight}" || -z "${current_subset}" ]]; then
            echo "ERROR: Incomplete @font-face data — Google Fonts CSS format may have changed." >&2
            exit 1
        fi

        # Validate extracted values to prevent path traversal or injection
        if [[ ! "${current_family}" =~ ^[A-Za-z0-9\ ]+$ ]]; then
            echo "ERROR: Unexpected font-family value: '${current_family}'" >&2
            exit 1
        fi
        if [[ ! "${current_weight}" =~ ^[0-9]+$ ]]; then
            echo "ERROR: Unexpected font-weight value: '${current_weight}'" >&2
            exit 1
        fi
        if [[ ! "${current_style}" =~ ^(normal|italic)$ ]]; then
            echo "ERROR: Unexpected font-style value: '${current_style}'" >&2
            exit 1
        fi
        if [[ ! "${current_subset}" =~ ^[a-z-]+$ ]]; then
            echo "ERROR: Unexpected subset value: '${current_subset}'" >&2
            exit 1
        fi

        # Derive directory and filename from font-family
        family_dir=$(echo "${current_family}" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
        mkdir -p "${FONTS_DIR}/${family_dir}"

        filename="${family_dir}-${current_weight}-${current_style}-${current_subset}.woff2"
        filepath="${FONTS_DIR}/${family_dir}/${filename}"

        curl -fsSL "${current_woff2_url}" -o "${filepath}"
        echo "    ${family_dir}/${filename}"

        # Build @font-face CSS block with relative path
        FONTS_CSS+="
@font-face {
  font-family: '${current_family}';
  font-style: ${current_style};
  font-weight: ${current_weight};
  font-display: swap;
  src: url('./${family_dir}/${filename}') format('woff2');
  unicode-range: ${current_range};
}
"
        # Reset woff2 URL for next block
        current_woff2_url=""
    fi
done < "${TMPFILE}"

# Write the generated fonts.css
echo "${FONTS_CSS}" > "${FONTS_DIR}/fonts.css"
echo "    fonts.css (generated)"

# Temp file cleaned up by EXIT trap

echo "==> Done. Vendor assets are in: ${VENDOR_DIR}"
