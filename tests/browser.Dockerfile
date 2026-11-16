FROM immersive-test
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && python3 -m venv --system-site-packages /opt/browser \
    && /opt/browser/bin/pip install --no-cache-dir playwright==1.56.0 \
    && /opt/browser/bin/playwright install --with-deps chromium firefox \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/browser/bin:${PATH}"
CMD ["python3", "-m", "pytest", "-q", "tests/test_browser.py"]
