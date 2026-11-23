# XFDL Mobile Downloader

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/atedickfer/SIMPCITY-DOWNLOADER-xfdl-mobile-)


A mobile-friendly Flask interface for the XFDL media scraper/downloader.

## Deploy on Render

1. Create a new **Blueprint** in Render and connect this repository.
2. Render reads `render.yaml`, installs the Python dependencies, and starts the app with Gunicorn.
3. The Blueprint generates a private `APP_PASSWORD` automatically. Find it under the service's **Environment** settings.
4. Open the service URL. When prompted for HTTP Basic Authentication, enter any username and the generated `APP_PASSWORD` as the password.

## Security and storage

- Every page and API endpoint requires the generated app password. Only `/healthz` is public for deployment health checks.
- Downloads and file browsing are restricted to the app's `downloads/` directory.
- Forum cookies are not saved in browser storage by default. The optional “Remember cookie on this device” setting stores it in that browser's local storage.
- Render's free web service uses ephemeral storage and may sleep when idle. Treat files on the server as temporary and download them to your phone before the service restarts or redeploys.
- The app runs as a single Gunicorn worker because job state and background tasks are held in process memory.

## Local run

```sh
python -m pip install -r requirements.txt
APP_PASSWORD='choose-a-long-random-password' python app.py
```

Open `http://127.0.0.1:8080` and use the password above when prompted.
