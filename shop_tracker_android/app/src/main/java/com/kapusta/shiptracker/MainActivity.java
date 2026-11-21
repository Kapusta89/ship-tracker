package com.kapusta.shiptracker;

import android.app.Activity;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Самостоятельная версия Ship Tracker для телефона.
 *
 * Python-часть (сбор данных, база, расчёты) поднимает локальный сервер на
 * 127.0.0.1, а это окно показывает его страницу — компьютер не нужен.
 */
public class MainActivity extends Activity {

    private WebView web;
    private int attempts = 0;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);

        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        web.setWebViewClient(new WebViewClient());
        web.setBackgroundColor(0xFF0B1622);
        setContentView(web);

        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }

        final String filesDir = getFilesDir().getAbsolutePath();
        new Thread(() -> {
            try {
                Python.getInstance().getModule("android_main").callAttr("start", filesDir);
            } catch (Throwable error) {
                pythonLog("start failed: " + error);
            }
        }, "python-start").start();

        loadWhenReady();
    }

    private void loadWhenReady() {
        attempts++;
        web.postDelayed(() -> {
            web.loadUrl("http://127.0.0.1:8080/");
            if (attempts < 20) {
                web.postDelayed(this::retryIfBlank, 1500);
            }
        }, 400);
    }

    private void retryIfBlank() {
        web.evaluateJavascript(
                "(document.body && document.body.innerText.trim().length > 0) ? 'ok' : 'blank'",
                value -> {
                    if (value != null && value.contains("blank")) {
                        loadWhenReady();
                    }
                });
    }

    private void pythonLog(String text) {
        try {
            android.util.Log.e("ShipTracker", text);
        } catch (Throwable ignored) {
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
