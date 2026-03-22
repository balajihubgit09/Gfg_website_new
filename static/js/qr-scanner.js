(() => {
    const hasBarcodeDetector = 'BarcodeDetector' in window;
    const isProbablyMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || '');

    async function listSupportedFormats() {
        if (!hasBarcodeDetector || typeof window.BarcodeDetector.getSupportedFormats !== 'function') {
            return ['qr_code'];
        }
        try {
            const formats = await window.BarcodeDetector.getSupportedFormats();
            return formats.includes('qr_code') ? ['qr_code'] : [];
        } catch {
            return ['qr_code'];
        }
    }

    function describeCameraError(error) {
        const errorName = error?.name || '';

        if (errorName === 'NotAllowedError' || errorName === 'PermissionDeniedError') {
            return 'Camera permission was denied. Allow camera access in the browser for this site and try again.';
        }
        if (errorName === 'NotFoundError' || errorName === 'DevicesNotFoundError') {
            return 'No camera was found on this device. Connect or enable a webcam and try again.';
        }
        if (errorName === 'NotReadableError' || errorName === 'TrackStartError') {
            return 'The camera is busy or blocked by another app. Close other camera apps and try again.';
        }
        if (errorName === 'OverconstrainedError' || errorName === 'ConstraintNotSatisfiedError') {
            return 'This camera does not support the requested capture mode. The scanner will retry with lighter settings.';
        }
        if (errorName === 'SecurityError') {
            return 'The browser blocked camera access for security reasons. Open the site with HTTPS or localhost and try again.';
        }
        if (errorName === 'AbortError') {
            return 'Camera startup was interrupted. Try opening the scanner again.';
        }

        return error?.message || 'Unable to open the camera on this device.';
    }

    async function createQrScanner({ container, onDetect, onStatus, videoConstraints } = {}) {
        if (!container) {
            throw new Error('Scanner container not found.');
        }
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
            throw new Error('Camera access is not supported in this browser.');
        }
        if (window.isSecureContext === false && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
            throw new Error('Camera access needs HTTPS or localhost on laptops and PCs.');
        }
        if (!hasBarcodeDetector) {
            throw new Error('Live camera QR scanning is not supported in this browser. Use the QR image upload option or paste the verification link manually.');
        }

        const formats = await listSupportedFormats();
        if (!formats.includes('qr_code')) {
            throw new Error('Live camera QR scanning is not supported in this browser. Use the QR image upload option or paste the verification link manually.');
        }

        const detector = new window.BarcodeDetector({ formats });
        const video = document.createElement('video');
        video.setAttribute('playsinline', 'true');
        video.muted = true;
        video.autoplay = true;
        video.className = 'qr-reader-video';

        const canvas = document.createElement('canvas');
        canvas.hidden = true;
        const context = canvas.getContext('2d', { willReadFrequently: true });

        container.innerHTML = '';
        container.appendChild(video);
        container.appendChild(canvas);

        let stream = null;
        let active = false;
        let frameHandle = 0;
        let detectionLock = false;
        let mustUseCanvasFallback = false;

        const stop = async () => {
            active = false;
            if (frameHandle) {
                window.cancelAnimationFrame(frameHandle);
                frameHandle = 0;
            }
            if (stream) {
                stream.getTracks().forEach((track) => track.stop());
                stream = null;
            }
            video.srcObject = null;
        };

        const detectFromCanvas = async () => {
            canvas.width = video.videoWidth || 1280;
            canvas.height = video.videoHeight || 720;
            context.drawImage(video, 0, 0, canvas.width, canvas.height);
            return detector.detect(canvas);
        };

        const detectCodes = async () => {
            if (!mustUseCanvasFallback) {
                try {
                    return await detector.detect(video);
                } catch {
                    mustUseCanvasFallback = true;
                }
            }
            return detectFromCanvas();
        };

        const scanFrame = async () => {
            if (!active) {
                return;
            }
            if (video.readyState >= HTMLMediaElement.HAVE_ENOUGH_DATA && !detectionLock) {
                detectionLock = true;
                try {
                    const codes = await detectCodes();
                    if (codes.length > 0) {
                        const code = codes.find((item) => item.rawValue) || codes[0];
                        if (code && code.rawValue) {
                            onStatus?.('QR detected.');
                            await onDetect(code.rawValue);
                            await stop();
                            return;
                        }
                    }
                } catch {
                    // Ignore transient detection failures while frames are stabilizing.
                } finally {
                    detectionLock = false;
                }
            }
            frameHandle = window.requestAnimationFrame(scanFrame);
        };

        const buildDefaultConstraints = () => {
            if (isProbablyMobile) {
                return {
                    facingMode: { ideal: 'environment' },
                    width: { ideal: 1280 },
                    height: { ideal: 720 },
                    frameRate: { ideal: 30, max: 60 },
                };
            }

            return {
                width: { ideal: 1280 },
                height: { ideal: 720 },
                frameRate: { ideal: 30, max: 30 },
            };
        };

        const buildFallbackConstraints = () => {
            const preferred = videoConstraints || buildDefaultConstraints();
            const withoutFacingMode = typeof preferred === 'object' && preferred !== null
                ? { ...preferred }
                : preferred;

            if (typeof withoutFacingMode === 'object' && withoutFacingMode !== null) {
                delete withoutFacingMode.facingMode;
            }

            return [
                preferred,
                withoutFacingMode,
                { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 24, max: 30 } },
                { width: { ideal: 960 }, height: { ideal: 540 } },
                true,
            ];
        };

        const requestCameraStream = async () => {
            const attempts = buildFallbackConstraints();
            let lastError = null;
            let sawPermissionError = false;
            let sawMissingDeviceError = false;
            let sawBusyCameraError = false;

            for (const candidate of attempts) {
                try {
                    return await navigator.mediaDevices.getUserMedia({ video: candidate, audio: false });
                } catch (error) {
                    lastError = error;
                    const errorName = error?.name || '';
                    if (errorName === 'NotAllowedError' || errorName === 'PermissionDeniedError' || errorName === 'SecurityError') {
                        sawPermissionError = true;
                        break;
                    }
                    if (errorName === 'NotFoundError' || errorName === 'DevicesNotFoundError') {
                        sawMissingDeviceError = true;
                    }
                    if (errorName === 'NotReadableError' || errorName === 'TrackStartError') {
                        sawBusyCameraError = true;
                    }
                }
            }

            if (!sawPermissionError && !sawMissingDeviceError && typeof navigator.mediaDevices.enumerateDevices === 'function') {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const videoDevices = devices.filter((device) => device.kind === 'videoinput');
                for (const videoDevice of videoDevices) {
                    if (!videoDevice.deviceId) {
                        continue;
                    }
                    try {
                        return await navigator.mediaDevices.getUserMedia({
                            video: {
                                deviceId: { exact: videoDevice.deviceId },
                                width: { ideal: 1280 },
                                height: { ideal: 720 },
                            },
                            audio: false,
                        });
                    } catch (error) {
                        lastError = error;
                        const errorName = error?.name || '';
                        if (errorName === 'NotAllowedError' || errorName === 'PermissionDeniedError' || errorName === 'SecurityError') {
                            sawPermissionError = true;
                            break;
                        }
                        if (errorName === 'NotReadableError' || errorName === 'TrackStartError') {
                            sawBusyCameraError = true;
                        }
                    }
                }
            }

            if (sawPermissionError) {
                throw new Error('Camera permission was denied or blocked. Open the site over HTTPS or localhost, allow camera access in the browser, and try again.');
            }
            if (sawMissingDeviceError) {
                throw new Error('No camera was found on this device. Connect or enable a webcam and try again.');
            }
            if (sawBusyCameraError) {
                throw new Error('The camera is already in use by another app. Close other apps using the webcam and try again.');
            }

            throw new Error(describeCameraError(lastError || new Error('No compatible camera could be opened.')));
        };

        const start = async () => {
            if (active) {
                return;
            }
            stream = await requestCameraStream();
            video.srcObject = stream;
            await video.play();
            active = true;
            onStatus?.('Scanner active. Point the camera at the QR code.');
            frameHandle = window.requestAnimationFrame(scanFrame);
        };

        return { start, stop };
    }

    window.createQrScanner = createQrScanner;
})();
