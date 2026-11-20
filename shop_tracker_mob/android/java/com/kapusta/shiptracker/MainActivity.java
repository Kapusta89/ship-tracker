package com.kapusta.shiptracker;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;

public class MainActivity extends Activity {

    private static final int DISCOVERY_PORT = 50505;
    private static final String PREFS = "ship_tracker";
    private static final String KEY_URL = "server_url";

    private WebView web;
    private FrameLayout root;
    private LinearLayout setup;
    private EditText address;
    private Button connect;
    private Button find;
    private TextView state;
    private ProgressBar bar;
    private String serverUrl = "";
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= 21) {
            getWindow().setStatusBarColor(Color.parseColor("#0b1622"));
            getWindow().setNavigationBarColor(Color.parseColor("#0b1622"));
        }
        SharedPreferences prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        serverUrl = prefs.getString(KEY_URL, "");

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0b1622"));
        setContentView(root);

        buildWeb();
        buildSetup();
        buildBar();

        if (serverUrl.isEmpty()) {
            showSetup("Введите адрес компьютера или найдите его в сети");
            discover();
        } else {
            loadServer(serverUrl);
        }
    }

    private void buildWeb() {
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b1622"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setMediaPlaybackRequiresUserGesture(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                bar.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, android.webkit.WebResourceError error) {
                if (Build.VERSION.SDK_INT >= 23 && request.isForMainFrame()) {
                    showSetup("Компьютер не отвечает. Проверьте, что Ship Tracker запущен.");
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int code, String description, String failingUrl) {
                if (Build.VERSION.SDK_INT < 23) {
                    showSetup("Компьютер не отвечает. Проверьте, что Ship Tracker запущен.");
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient());
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void buildBar() {
        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setIndeterminate(true);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 6);
        lp.gravity = Gravity.TOP;
        root.addView(bar, lp);
        bar.setVisibility(View.GONE);
    }

    private void buildSetup() {
        setup = new LinearLayout(this);
        setup.setOrientation(LinearLayout.VERTICAL);
        setup.setGravity(Gravity.CENTER_VERTICAL);
        setup.setPadding(dp(22), dp(22), dp(22), dp(22));
        setup.setBackgroundColor(Color.parseColor("#0b1622"));

        TextView title = new TextView(this);
        title.setText("Ship Tracker");
        title.setTextSize(26);
        title.setTextColor(Color.parseColor("#e8f1fa"));
        setup.addView(title);

        TextView hint = new TextView(this);
        hint.setText("Приложение показывает суда с компьютера: он должен быть включён, "
                + "Ship Tracker Mobile запущен, телефон — в той же Wi-Fi сети. "
                + "Ключи и подключения к источникам данных настраиваются в версии "
                + "Ship Tracker, которая запускается через run.bat.");
        hint.setTextSize(14);
        hint.setTextColor(Color.parseColor("#7d95ad"));
        hint.setPadding(0, dp(8), 0, dp(18));
        setup.addView(hint);

        address = new EditText(this);
        address.setHint("http://192.168.1.35:8080");
        address.setText(serverUrl);
        address.setTextSize(16);
        address.setSingleLine(true);
        address.setTextColor(Color.parseColor("#e8f1fa"));
        address.setHintTextColor(Color.parseColor("#6d8299"));
        address.setImeOptions(EditorInfo.IME_ACTION_GO);
        setup.addView(address, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        connect = new Button(this);
        connect.setText("Подключиться");
        connect.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String value = address.getText().toString().trim();
                if (value.isEmpty()) {
                    Toast.makeText(MainActivity.this, "Введите адрес компьютера", Toast.LENGTH_SHORT).show();
                    return;
                }
                if (!value.startsWith("http://") && !value.startsWith("https://")) {
                    value = "http://" + value;
                }
                loadServer(value);
            }
        });
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        buttonParams.topMargin = dp(12);
        setup.addView(connect, buttonParams);

        find = new Button(this);
        find.setText("Найти компьютер в сети");
        find.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                discover();
            }
        });
        setup.addView(find, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        state = new TextView(this);
        state.setTextSize(13);
        state.setTextColor(Color.parseColor("#7d95ad"));
        state.setPadding(0, dp(14), 0, 0);
        setup.addView(state);

        root.addView(setup, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setup.setVisibility(View.GONE);
    }

    private int dp(int value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }

    private void showSetup(String message) {
        bar.setVisibility(View.GONE);
        setup.setVisibility(View.VISIBLE);
        if (message != null) {
            state.setText(message);
        }
        if (!serverUrl.isEmpty() && address.getText().toString().trim().isEmpty()) {
            address.setText(serverUrl);
        }
    }

    private void loadServer(String url) {
        serverUrl = url;
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_URL, url).apply();
        setup.setVisibility(View.GONE);
        bar.setVisibility(View.VISIBLE);
        if (!web.getUrl().equals(url) && !url.equals(web.getUrl() + "/")) {
            web.loadUrl(url);
        } else {
            web.reload();
        }
    }

    private void discover() {
        state.setText("Ищу Ship Tracker в Wi-Fi сети…");
        find.setEnabled(false);
        connect.setEnabled(false);

        new Thread(new Runnable() {
            @Override
            public void run() {
                String found = null;
                DatagramSocket socket = null;
                try {
                    socket = new DatagramSocket();
                    socket.setBroadcast(true);
                    socket.setSoTimeout(900);
                    byte[] probe = "SHIPTRACKER?".getBytes("UTF-8");
                    InetAddress broadcast = InetAddress.getByName("255.255.255.255");
                    long deadline = System.currentTimeMillis() + 4000;
                    while (System.currentTimeMillis() < deadline && found == null) {
                        socket.send(new DatagramPacket(probe, probe.length, broadcast, DISCOVERY_PORT));
                        byte[] buffer = new byte[1024];
                        DatagramPacket reply = new DatagramPacket(buffer, buffer.length);
                        try {
                            socket.receive(reply);
                            String text = new String(buffer, 0, reply.getLength(), "UTF-8");
                            JSONObject json = new JSONObject(text);
                            String host = reply.getAddress().getHostAddress();
                            int port = json.optInt("port", 8080);
                            if ("ship_tracker_mob".equals(json.optString("app"))) {
                                found = "http://" + host + ":" + port;
                            }
                        } catch (SocketTimeoutException ignored) {
                            // следующий круг широковещательного запроса
                        }
                    }
                } catch (Exception ignored) {
                    found = null;
                } finally {
                    if (socket != null) {
                        socket.close();
                    }
                }

                final String result = found;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        find.setEnabled(true);
                        connect.setEnabled(true);
                        if (result != null) {
                            address.setText(result);
                            state.setText("Нашёл: " + result);
                            loadServer(result);
                        } else {
                            state.setText("Компьютер не найден. Проверьте Wi-Fi и адрес с окна "
                                    + "Ship Tracker Mobile на компьютере.");
                        }
                    }
                });
            }
        }).start();
    }

    @Override
    public void onResume() {
        super.onResume();
        if (!serverUrl.isEmpty() && setup.getVisibility() == View.GONE) {
            web.reload();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
