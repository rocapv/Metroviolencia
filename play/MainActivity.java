package es.metroviu.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Insets;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

// MetroViu (Google Play). Muestra https://metro.pr1.es/ en una WebView.
//  - Solo HTTPS (certificado de Let's Encrypt): nada de LAN, HTTP ni cert autofirmado.
//  - Hoy la web es abierta. Si el servidor vuelve a exigir token (403), la app lo pide,
//    lo guarda y lo manda una vez como ?token= (después queda en cookie).
//  - Ubicación nativa inyectada en window.mvPosicio (web/xat.js); no sale del móvil.
//  - Android 15/16: pantalla de borde a borde (márgenes con los insets) y "atrás" predictivo.
public class MainActivity extends Activity {

    private static final String BASE = "https://metro.pr1.es/";
    private static final String HOST = "metro.pr1.es";

    private WebView web;
    private FrameLayout root;
    private boolean errorMostrat = false;
    private OnBackInvokedCallback backCb;

    private static final int REQ_UBICACIO = 77;
    private static final int REQ_UBICACIO_NATIVA = 78;
    private String geoOrigen;
    private GeolocationPermissions.Callback geoCallback;
    private LocationManager locMgr;
    private boolean ubicacioActiva = false;
    private boolean ubicacioVolguda = false;   // la web la ha pedido: se reanuda en onResume

    private final LocationListener locListener = new LocationListener() {
        @Override public void onLocationChanged(Location l) {
            final String js = "window.mvPosicio && window.mvPosicio(" + l.getLatitude() + "," + l.getLongitude()
                    + "," + l.getAccuracy() + ")";
            runOnUiThread(new Runnable() { @Override public void run() { if (web != null) web.evaluateJavascript(js, null); } });
        }
        @Override public void onProviderDisabled(String p) {}
        @Override public void onProviderEnabled(String p) {}
        @Override public void onStatusChanged(String p, int s, Bundle b) {}
    };

    // nombre que espera web/xat.js
    private final class Natiu {
        @JavascriptInterface
        public void demanaUbicacio() {
            runOnUiThread(new Runnable() { @Override public void run() { ubicacioVolguda = true; iniciaUbicacio(); } });
        }
        @JavascriptInterface
        public void aturaUbicacio() {
            runOnUiThread(new Runnable() { @Override public void run() { ubicacioVolguda = false; MainActivity.this.aturaUbicacio(); } });
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(18, 38, 66));
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
        aplicaInsets();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setGeolocationEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.addJavascriptInterface(new Natiu(), "MetroviolenciaNatiu");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback cb) {
                if (!esNostre(Uri.parse(origin))) { cb.invoke(origin, false, false); return; }
                if (Build.VERSION.SDK_INT >= 23 && !tePermisUbicacio()) {
                    geoOrigen = origin; geoCallback = cb;
                    requestPermissions(new String[] { Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION }, REQ_UBICACIO);
                } else {
                    cb.invoke(origin, true, false);
                }
            }
        });

        if (Build.VERSION.SDK_INT >= 33) {
            backCb = new OnBackInvokedCallback() {
                @Override public void onBackInvoked() {
                    if (web.canGoBack()) web.goBack(); else finish();
                }
            };
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, backCb);
        }

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else carrega();
    }

    // De borde a borde (obligatorio con targetSdk 35+): deja libres barras y recortes.
    private void aplicaInsets() {
        if (Build.VERSION.SDK_INT < 30) return;
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override public WindowInsets onApplyWindowInsets(View v, WindowInsets in) {
                Insets b = in.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                        | WindowInsets.Type.ime());
                v.setPadding(b.left, b.top, b.right, b.bottom);
                return WindowInsets.CONSUMED;
            }
        });
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("mv", Context.MODE_PRIVATE);
    }

    private void carrega() {
        errorMostrat = false;
        String token = prefs().getString("token", "");
        web.loadUrl(token.isEmpty() ? BASE : BASE + "?token=" + Uri.encode(token));
    }

    private static boolean esNostre(Uri u) {
        return u != null && "https".equals(u.getScheme()) && HOST.equalsIgnoreCase(u.getHost());
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (esNostre(u)) return false;
            // enlaces externos: al navegador, fuera de la app
            try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (ActivityNotFoundException e) { }
            return true;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) mostraError();
        }

        @Override
        public void onReceivedHttpError(WebView v, WebResourceRequest req, WebResourceResponse resp) {
            if (!req.isForMainFrame() || resp == null) return;
            int code = resp.getStatusCode();
            if (code == 403) {
                prefs().edit().remove("token").apply();
                demanaToken();
            } else if (code >= 500) {
                mostraError();
            }
        }
    }

    private void mostraError() {
        if (errorMostrat || isFinishing()) return;
        errorMostrat = true;
        web.loadUrl("about:blank");
        new AlertDialog.Builder(this)
                .setTitle("MetroViu")
                .setMessage("No es pot connectar amb el servidor. Comprova la connexió.")
                .setCancelable(false)
                .setPositiveButton("Torna-ho a provar", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) { carrega(); }
                })
                .setNegativeButton("Ix", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) { finish(); }
                })
                .show();
    }

    private void demanaToken() {
        if (isFinishing()) return;
        final EditText et = new EditText(this);
        et.setHint("Codi d'accés");
        et.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        new AlertDialog.Builder(this)
                .setTitle("MetroViu")
                .setMessage("Aquest servidor demana un codi d'accés.")
                .setView(et)
                .setCancelable(false)
                .setPositiveButton("Entra", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) {
                        String t = et.getText().toString().trim();
                        if (!t.isEmpty()) prefs().edit().putString("token", t).apply();
                        carrega();
                    }
                })
                .setNegativeButton("Ix", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) { finish(); }
                })
                .show();
    }

    private boolean tePermisUbicacio() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private void iniciaUbicacio() {
        if (Build.VERSION.SDK_INT >= 23 && !tePermisUbicacio()) {
            requestPermissions(new String[] { Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION }, REQ_UBICACIO_NATIVA);
            return;
        }
        if (ubicacioActiva) return;
        try {
            if (locMgr == null) locMgr = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
            for (String prov : new String[] { LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER }) {
                if (locMgr.isProviderEnabled(prov)) {
                    locMgr.requestLocationUpdates(prov, 2000, 5, locListener);
                    Location last = locMgr.getLastKnownLocation(prov);
                    if (last != null) locListener.onLocationChanged(last);
                    ubicacioActiva = true;
                }
            }
            if (!ubicacioActiva) web.evaluateJavascript(
                    "window.mvPosicioError && window.mvPosicioError('Activa la ubicació del mòbil.')", null);
        } catch (SecurityException e) {
            web.evaluateJavascript("window.mvPosicioError && window.mvPosicioError('Sense permís d\\'ubicació.')", null);
        }
    }

    private void aturaUbicacio() {
        if (locMgr != null && ubicacioActiva) locMgr.removeUpdates(locListener);
        ubicacioActiva = false;
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        boolean ok = false;
        for (int r : res) if (r == PackageManager.PERMISSION_GRANTED) ok = true;   // fina o aproximada
        if (req == REQ_UBICACIO_NATIVA) {
            if (ok) iniciaUbicacio();
            else web.evaluateJavascript(
                    "window.mvPosicioError && window.mvPosicioError('Has denegat la ubicació. Pots triar una estació a mà.')", null);
            return;
        }
        if (req != REQ_UBICACIO || geoCallback == null) return;
        geoCallback.invoke(geoOrigen, ok, false);
        geoCallback = null; geoOrigen = null;
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (ubicacioVolguda && Build.VERSION.SDK_INT >= 23 && tePermisUbicacio()) iniciaUbicacio();
    }

    @Override
    protected void onPause() {
        aturaUbicacio();   // sin ubicación en segundo plano
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onDestroy() {
        aturaUbicacio();
        if (Build.VERSION.SDK_INT >= 33 && backCb != null)
            getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(backCb);
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }

    // Android < 13 (sin OnBackInvokedCallback)
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (Build.VERSION.SDK_INT < 33 && keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
