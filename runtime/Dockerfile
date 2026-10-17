# Sandbox computer: a small desktop on an Xvnc display with a Plank dock
# (Chromium, Files, Terminal), driven by driver.mjs and viewable over VNC.
FROM mcr.microsoft.com/playwright:v1.62.1-noble

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server \
        xfwm4 plank thunar xfce4-terminal \
        dbus-x11 x11-utils x11-xserver-utils xdotool scrot feh \
        adwaita-icon-theme librsvg2-common \
        python3-pip python3-venv jq unzip poppler-utils \
    && rm -rf /var/lib/apt/lists/*
# Document converters for attached files: markitdown (PDF, Word, Excel,
# PowerPoint to Markdown) for the bot's own use, and pdftotext above.
RUN pip3 install --no-cache-dir --break-system-packages "markitdown[docx,pdf,pptx,xls,xlsx]>=0.1.8"

WORKDIR /opt/cubicle-computer

COPY package.json ./
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN npm install --omit=dev --ignore-scripts

COPY driver.mjs start.sh desktop.sh install-packages.sh chromium-browser make_wallpaper.py ./
COPY skel ./skel
COPY applications ./applications
# The dock's Chromium launcher reuses the browser Playwright started, so the
# wrapper and the launcher icon need the exact build Playwright resolved.
RUN CHROME="$(node -e "console.log(require('playwright').chromium.executablePath())")" \
    && sed -i "s#__CHROME__#${CHROME}#" chromium-browser \
    && sed -i "s#__CHROME__#${CHROME}#; s#__CHROME_ICON__#$(dirname "${CHROME}")/product_logo_48.png#" applications/cubicle-chromium.desktop \
    && install -m 0755 chromium-browser /usr/local/bin/chromium-browser \
    && install -m 0644 applications/*.desktop /usr/share/applications/ \
    && chmod 0755 start.sh desktop.sh install-packages.sh \
    && python3 make_wallpaper.py wallpaper \
    && mkdir -p /workspace /var/lib/cubicle/apt \
    && chown -R pwuser:pwuser /opt/cubicle-computer /workspace \
    # The scripts root runs (at boot, and install-packages.sh through a root
    # docker exec) and the directory holding them must not be writable by
    # the sandbox user, or it could put its own code where root will run it.
    && chown root:root /opt/cubicle-computer /opt/cubicle-computer/start.sh \
        /opt/cubicle-computer/desktop.sh /opt/cubicle-computer/install-packages.sh

# No USER: start.sh begins as root to restore the computer's packages and
# own the mounted home, then drops to pwuser (with no capabilities left)
# before the desktop and driver start.
ENV PORT=3000 \
    WORKSPACE=/workspace \
    DISPLAY=:1 \
    HOME=/home/pwuser \
    VIEWPORT_WIDTH=1280 \
    VIEWPORT_HEIGHT=720
EXPOSE 3000

ENTRYPOINT ["/opt/cubicle-computer/start.sh"]
