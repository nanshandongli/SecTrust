package com.example.remoteconsole;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.View;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;

/**
 * 协作审计台 · 手机远程控制台
 *
 * 这是一个极简的 WebView 外壳：它把电脑端「协作审计台」的手机控制台页面
 * （http://<电脑IP>:<端口>/mobile）加载进来，从而把手机变成一个遥控器。
 * 所有真正的控制逻辑都在电脑端产品里，本 App 不做任何业务判断。
 *
 * 首次启动会让你输入电脑端服务地址（局域网 IP + 端口），之后保存在本地，
 * 点顶栏「地址」可重新输入。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "remote_console_prefs";
    private static final String KEY_URL = "server_url";
    private static final String MOBILE_PATH = "/mobile";

    private WebView webView;
    private SharedPreferences sp;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        webView = findViewById(R.id.webview);

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        // 让页面里的链接/跳转仍在 WebView 内打开，不弹出外部浏览器
        webView.setWebViewClient(new WebViewClient());

        Button btnUrl = findViewById(R.id.btn_url);
        btnUrl.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                promptForUrl();
            }
        });

        String url = sp.getString(KEY_URL, "");
        if (url == null || url.trim().isEmpty()) {
            promptForUrl();
        } else {
            loadUrl(url.trim());
        }
    }

    /** 拼接成 /mobile 页面地址并加载 */
    private void loadUrl(String base) {
        String u = base.trim();
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            u = "http://" + u;
        }
        // 去掉结尾斜杠，再补上移动端路径，避免重复拼接
        u = u.replaceAll("/+$", "");
        if (!u.endsWith(MOBILE_PATH)) {
            u = u + MOBILE_PATH;
        }
        webView.loadUrl(u);
    }

    /** 弹出输入框，保存并连接服务器地址 */
    private void promptForUrl() {
        AlertDialog.Builder b = new AlertDialog.Builder(this);
        b.setTitle("服务器地址");
        b.setMessage("输入电脑端协作审计台地址（局域网 IP + 端口），例如：\nhttp://192.168.1.50:8088");

        final EditText input = new EditText(this);
        input.setHint("http://192.168.1.50:8088");
        String cur = sp.getString(KEY_URL, "");
        if (cur != null) input.setText(cur);
        b.setView(input);

        b.setPositiveButton("连接", new android.content.DialogInterface.OnClickListener() {
            @Override
            public void onClick(android.content.DialogInterface d, int which) {
                String v = input.getText().toString().trim();
                if (!v.isEmpty()) {
                    sp.edit().putString(KEY_URL, v).apply();
                    loadUrl(v);
                }
            }
        });
        b.setNegativeButton("取消", null);
        b.show();
    }

    /** 返回键：先在网页内后退，再到退出 App */
    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
