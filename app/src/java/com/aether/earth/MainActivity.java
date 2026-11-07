package com.aether.earth;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.GeolocationPermissions;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.TextView;

import java.io.IOException;
import java.io.InputStream;

/**
 * AETHER Earth Intelligence — WebView shell.
 *
 * Renders the bundled PWA (assets/www, built by the web team) which reads the
 * real /v1 backend API. This native shell contains NO weather/earthquake/
 * tsunami data of any kind — no hardcoded sample numbers.
 */
public class MainActivity extends Activity {

    private static final int REQ_GEO = 1001;
    private static final String CHANNEL_ID = "aether_alerts";

    private WebView webView;
    private PullRefreshLayout refreshLayout;

    private String pendingGeoOrigin;
    private GeolocationPermissions.Callback pendingGeoCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Swap splash theme for the real theme once the window is up.
        setTheme(R.style.Theme_Aether);
        super.onCreate(savedInstanceState);

        createNotificationChannel(); // scaffolding only; no notifications are posted

        if (!hasBundledContent()) {
            showError(getString(R.string.error_missing_assets), false);
            return;
        }

        setContentView(R.layout.activity_main);
        refreshLayout = findViewById(R.id.refresh_layout);
        webView = findViewById(R.id.webview);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setGeolocationEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setAllowFileAccess(true);
        // Bundled PWA loads i18n JSON + data snapshots via fetch() from
        // file:///android_asset/. Without these, file:// XHR is blocked and
        // the app boots with no text and no data ("nothing shows up").
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        if (Build.VERSION.SDK_INT >= 29) {
            // Web content follows the system dark mode.
            s.setAlgorithmicDarkeningAllowed(true);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                refreshLayout.setRefreshing(false);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                       WebResourceError error) {
                if (request.isForMainFrame()) {
                    showError(getString(R.string.error_load_failed), true);
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode,
                                       String description, String failingUrl) {
                showError(getString(R.string.error_load_failed), true);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleUrl(request.getUrl().toString());
            }

            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleUrl(url);
            }

            private boolean handleUrl(String url) {
                if (url.startsWith("http://") || url.startsWith("https://")
                        || url.startsWith("file://")) {
                    return false; // keep in WebView
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback callback) {
                if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED) {
                    callback.invoke(origin, true, false);
                } else {
                    pendingGeoOrigin = origin;
                    pendingGeoCallback = callback;
                    requestPermissions(
                            new String[]{Manifest.permission.ACCESS_FINE_LOCATION}, REQ_GEO);
                }
            }
        });

        refreshLayout.setOnRefreshListener(new PullRefreshLayout.OnRefreshListener() {
            @Override
            public void onRefresh() {
                webView.reload();
            }
        });

        webView.loadUrl("file:///android_asset/www/index.html");
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                          int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_GEO && pendingGeoCallback != null) {
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            pendingGeoCallback.invoke(pendingGeoOrigin, granted, false);
            pendingGeoCallback = null;
            pendingGeoOrigin = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    /** True when the web team's bundled PWA entry point exists in assets. */
    private boolean hasBundledContent() {
        InputStream in = null;
        try {
            in = getAssets().open("www/index.html");
            return true;
        } catch (IOException e) {
            return false;
        } finally {
            if (in != null) {
                try {
                    in.close();
                } catch (IOException ignored) {
                }
            }
        }
    }

    /** Honest error screen — shown when assets are missing or the page fails to load. */
    private void showError(String message, boolean retryAllowed) {
        setContentView(R.layout.activity_error);
        TextView msg = findViewById(R.id.error_message);
        msg.setText(message);
        Button retry = findViewById(R.id.retry_button);
        if (retryAllowed) {
            retry.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    recreate();
                }
            });
        } else {
            retry.setVisibility(View.GONE);
        }
    }

    /**
     * Notification channel scaffolding. The channel exists so future official
     * alert notifications have a home; nothing is posted by this build.
     */
    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null && nm.getNotificationChannel(CHANNEL_ID) == null) {
                NotificationChannel ch = new NotificationChannel(
                        CHANNEL_ID,
                        getString(R.string.channel_name),
                        NotificationManager.IMPORTANCE_HIGH);
                ch.setDescription(getString(R.string.channel_desc));
                nm.createNotificationChannel(ch);
            }
        }
    }
}
