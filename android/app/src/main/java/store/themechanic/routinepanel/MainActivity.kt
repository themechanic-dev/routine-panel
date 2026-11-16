package store.themechanic.routinepanel

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Typeface
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Routine Panel on the phone: the panel of the computer it was paired with, read only.
 *
 * Pairing: the panel on the computer shows a code (Phone → Pair a phone); this app scans it, or the phone's camera
 * opens it as a routinepanel://pair link. The code holds the computer's addresses (Tailscale first, then the home
 * network), its port and a key of this phone's own. The app keeps them and opens the computer's panel page in a web
 * view, with the key after "#" as the window on the computer does. Which address answers is checked on every start
 * and whenever the phone changes network: at home the home network, away from home Tailscale.
 */
class MainActivity : Activity() {

    private lateinit var prefs: SharedPreferences
    private lateinit var root: FrameLayout
    private var web: WebView? = null
    private var shownAddress: String? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var connecting = false
    private var recheck: Runnable? = null

    // --- life ----------------------------------------------------------------------------------------------------
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("pairing", Context.MODE_PRIVATE)
        root = FrameLayout(this)
        root.fitsSystemWindows = false
        root.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars() or
                android.view.WindowInsets.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(root)
        if (!handlePairLink(intent)) start()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePairLink(intent)
    }

    override fun onStart() {
        super.onStart()
        watchNetwork()
    }

    override fun onStop() {
        super.onStop()
        networkCallback?.let { (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(it) }
        networkCallback = null
    }

    override fun onDestroy() {
        web?.destroy()
        io.shutdownNow()
        super.onDestroy()
    }

    private fun start() {
        if (paired()) connect() else showWelcome()
    }

    // --- pairing ---------------------------------------------------------------------------------------------------
    private data class Pairing(val name: String, val port: Int, val addresses: List<String>, val key: String)

    private fun paired(): Boolean = prefs.getString("key", null) != null

    private fun stored(): Pairing? {
        val key = prefs.getString("key", null) ?: return null
        val addresses = (prefs.getString("addresses", "") ?: "").split(",").filter { it.isNotBlank() }
        return Pairing(prefs.getString("name", "the computer") ?: "the computer", prefs.getInt("port", 47613), addresses, key)
    }

    /** A routinepanel://pair link from the camera or the scanner; whether the intent was one. */
    private fun handlePairLink(intent: Intent?): Boolean {
        val data = intent?.data ?: return false
        if (data.scheme != "routinepanel") return false
        intent.data = null // not again on the next start
        val pairing = parse(data.toString())
        if (pairing == null) {
            showMessage("This code is not a Routine Panel pairing code.")
            return true
        }
        AlertDialog.Builder(this)
            .setTitle("Pair with ${pairing.name}?")
            .setMessage("This phone will show the routines of ${pairing.name}. It only reads: it cannot start, stop or change anything.")
            .setPositiveButton("Pair") { _, _ -> save(pairing) }
            .setNegativeButton("Cancel") { _, _ -> start() }
            .setOnCancelListener { start() }
            .show()
        return true
    }

    private fun parse(text: String): Pairing? {
        val uri = try { Uri.parse(text) } catch (e: Exception) { return null }
        if (uri.scheme != "routinepanel" || uri.host != "pair") return null
        val key = uri.getQueryParameter("k") ?: return null
        val port = uri.getQueryParameter("p")?.toIntOrNull() ?: return null
        val name = (uri.getQueryParameter("n") ?: "the computer").take(60)
        val addresses = (uri.getQueryParameter("a") ?: "").split(",").map { it.trim() }
            .filter { Regex("""\d{1,3}(\.\d{1,3}){3}""").matches(it) }
        if (!Regex("""[A-Za-z0-9_-]{20,100}""").matches(key) || port !in 1..65535 || addresses.isEmpty()) return null
        return Pairing(name, port, addresses, key)
    }

    private fun save(pairing: Pairing) {
        prefs.edit()
            .putString("key", pairing.key)
            .putString("name", pairing.name)
            .putInt("port", pairing.port)
            .putString("addresses", pairing.addresses.joinToString(","))
            .apply()
        dropWeb()
        connect()
    }

    private fun forget() {
        prefs.edit().clear().apply()
        dropWeb()
        showWelcome()
    }

    private fun scan() {
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAutoZoom()
            .build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { barcode ->
                val pairing = barcode.rawValue?.let { parse(it) }
                if (pairing == null) showMessage("This code is not a Routine Panel pairing code.") else save(pairing)
            }
            .addOnFailureListener { e ->
                Toast.makeText(this, "The scanner could not open (${e.message}). Scan the code with the phone's camera instead.",
                    Toast.LENGTH_LONG).show()
            }
    }

    // --- reaching the computer -----------------------------------------------------------------------------------
    /** Finds the address that answers (the home network first when on Wi-Fi) and opens the panel there. */
    private fun connect(quiet: Boolean = false) {
        val pairing = stored() ?: return showWelcome()
        if (connecting) return
        connecting = true
        if (!quiet && web == null) showProgress("Looking for ${pairing.name}…")
        val order = ordered(pairing.addresses)
        io.execute {
            var found: String? = null
            var refused = false
            var learned: List<String> = emptyList()
            for (address in order) {
                val (status, answer) = ping(address, pairing)
                if (status == 401) refused = true
                if (answer != null) {
                    found = address
                    val list = answer.optJSONArray("addresses")
                    if (list != null) learned = (0 until list.length()).map { list.optString(it) }.filter { it.isNotBlank() }
                    break
                }
            }
            main.post {
                connecting = false
                if (isFinishing || isDestroyed) return@post
                if (found == null && refused) {
                    showRemoved(pairing) // the computer answered, but no longer knows this phone's key
                    return@post
                }
                if (found == null) {
                    if (web == null || !quiet) showUnreachable(pairing, order)
                    return@post
                }
                if (learned.isNotEmpty() && learned.toSet() != pairing.addresses.toSet()) {
                    prefs.edit().putString("addresses", learned.joinToString(",")).apply() // the computer's addresses changed
                }
                if (web == null || shownAddress != found) showPanel(found, pairing)
            }
        }
    }

    private fun ordered(addresses: List<String>): List<String> {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val onWifi = caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true ||
            caps?.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) == true
        val tailscale = addresses.filter { it.startsWith("100.") }
        val home = addresses.filter { !it.startsWith("100.") }
        return if (onWifi) home + tailscale else tailscale + home
    }

    /** (HTTP status or 0 when nothing answered, the computer's answer when it knows this phone). */
    private fun ping(address: String, pairing: Pairing): Pair<Int, JSONObject?> = try {
        val connection = URL("http://$address:${pairing.port}/api/ping").openConnection() as HttpURLConnection
        connection.connectTimeout = 1800
        connection.readTimeout = 2500
        connection.setRequestProperty("X-Panel-Key", pairing.key)
        connection.setRequestProperty("User-Agent", userAgent(null))
        try {
            val status = connection.responseCode
            if (status == 200) {
                val body = connection.inputStream.bufferedReader().use { it.readText() }
                status to JSONObject(body).takeIf { it.optString("role") == "phone" }
            } else status to null
        } finally {
            connection.disconnect()
        }
    } catch (e: Exception) {
        0 to null
    }

    /** Looks again when the phone changes network (home Wi-Fi ↔ mobile data and Tailscale). */
    private fun watchNetwork() {
        if (networkCallback != null) return
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = later()
            override fun onLost(network: Network) = later()
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = later()
        }
        cm.registerDefaultNetworkCallback(callback)
        networkCallback = callback
    }

    private fun later() {
        main.post {
            recheck?.let { main.removeCallbacks(it) }
            val again = Runnable { if (paired()) connect(quiet = true) }
            recheck = again
            main.postDelayed(again, 2500)
        }
    }

    // --- the panel -------------------------------------------------------------------------------------------------
    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    private fun showPanel(address: String, pairing: Pairing) {
        dropWeb()
        val origin = "http://$address:${pairing.port}"
        val view = WebView(this)
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true // the page remembers its theme and its list/grid choice
        view.settings.userAgentString = userAgent(view.settings.userAgentString)
        view.settings.allowFileAccess = false
        view.settings.allowContentAccess = false
        view.addJavascriptInterface(Bridge(), "RoutinePanelApp")
        view.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                if (url.startsWith("$origin/")) return false
                // anything else opens outside the app: the bridge is for the computer's panel only
                try { startActivity(Intent(Intent.ACTION_VIEW, request.url)) } catch (e: Exception) { /* nothing opens it */ }
                return true
            }

            override fun onReceivedError(v: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) main.post { connect() }
            }
        }
        root.removeAllViews()
        root.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        web = view
        shownAddress = address
        view.loadUrl("$origin/#${pairing.key}")
    }

    private fun dropWeb() {
        web?.let { root.removeView(it); it.destroy() }
        web = null
        shownAddress = null
    }

    /** What the panel's page may ask of the app: to show the app's menu, and to look again when its key is refused. */
    private inner class Bridge {
        @JavascriptInterface
        fun menu() {
            main.post { showMenu() }
        }

        @JavascriptInterface
        fun refused() {
            main.post { dropWeb(); connect() }
        }
    }

    private fun showMenu() {
        val name = stored()?.name ?: "the computer"
        val via = shownAddress?.let { if (it.startsWith("100.")) "Tailscale ($it)" else "the home network ($it)" } ?: "—"
        // A dialog with a message shows no list (Android draws one or the other), so the words go in its title.
        val heading = TextView(this).apply {
            text = "Paired with $name\nConnected over $via"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setLineSpacing(0f, 1.2f)
            setPadding(dp(24), dp(20), dp(24), dp(8))
        }
        AlertDialog.Builder(this)
            .setCustomTitle(heading)
            .setItems(arrayOf("Reload", "Scan a new code", "Forget this computer")) { _, which ->
                when (which) {
                    0 -> { dropWeb(); connect() }
                    1 -> scan()
                    2 -> AlertDialog.Builder(this)
                        .setTitle("Forget $name?")
                        .setMessage("This phone stops showing its routines. To show them again, pair again with a new code. " +
                            "To take this phone's key away on the computer too, remove it there (Phone → Remove).")
                        .setPositiveButton("Forget") { _, _ -> forget() }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            }
            .setNegativeButton("Close", null)
            .show()
    }

    /**
     * The web view's own User-Agent with this app and the phone's name added. The name is percent-encoded (UTF-8): the
     * web view turns every letter outside Latin-1 into "?" in its headers, so a Greek phone name reached the computer
     * as "?? S22 Ultra ???" from the page's requests, while the app's own check had sent it whole (measured
     * 2026-11-16). Encoded, it is the same text on both ways.
     */
    private fun userAgent(base: String?): String {
        val name = (Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL ?: "Android phone")
            .replace(Regex("""[^\p{L}\p{N} ._-]"""), "").trim().take(40).ifEmpty { "Android phone" }
        val encoded = java.net.URLEncoder.encode(name, "UTF-8").replace("+", "%20")
        val version = packageManager.getPackageInfo(packageName, 0).versionName ?: "0"
        return listOfNotNull(base, "RoutinePanelApp/$version ($encoded)").joinToString(" ")
    }

    // --- the app's own screens -----------------------------------------------------------------------------------
    private fun page(vararg views: View) {
        dropWeb()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            val pad = dp(28)
            setPadding(pad, dp(56), pad, pad)
        }
        views.forEach { column.addView(it) }
        val scroll = android.widget.ScrollView(this).apply { addView(column) }
        root.removeAllViews()
        root.addView(scroll, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    private fun icon() = ImageView(this).apply {
        setImageResource(R.mipmap.ic_launcher)
        layoutParams = LinearLayout.LayoutParams(dp(96), dp(96)).apply { bottomMargin = dp(18) }
    }

    private fun title(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 24f)
        typeface = Typeface.create(typeface, Typeface.BOLD)
        gravity = Gravity.CENTER
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { bottomMargin = dp(14) }
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
        setLineSpacing(0f, 1.25f)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { bottomMargin = dp(22) }
    }

    private fun button(text: String, action: () -> Unit) = Button(this).apply {
        this.text = text
        isAllCaps = false
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
        setOnClickListener { action() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { bottomMargin = dp(10) }
    }

    private fun showWelcome() = page(
        icon(),
        title("Routine Panel"),
        body("See the routines of your computer on this phone: what runs now, what waits, what broke, and how many " +
            "tokens each one's last cycle used. It only reads.\n\n" +
            "On the computer, open Routine Panel and click Phone, then Pair a phone. Scan the code it shows."),
        button("Scan the code") { scan() },
        signature(),
    )

    /** HOME LAB · by the mechanic, under the version: quiet, as in the panel's Help. */
    private fun signature(): View {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { topMargin = dp(36) }
            alpha = 0.6f
        }
        val mark = TextView(this).apply {
            text = "HOME LAB"
            letterSpacing = 0.25f
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            typeface = Typeface.create(typeface, Typeface.BOLD)
            setPadding(dp(10), dp(3), dp(10), dp(3))
            background = android.graphics.drawable.GradientDrawable().apply {
                setStroke(dp(1), currentTextColor)
                cornerRadius = dp(4).toFloat()
            }
        }
        val by = TextView(this).apply {
            val version = packageManager.getPackageInfo(packageName, 0).versionName ?: ""
            text = "by the mechanic · $version"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setPadding(0, dp(6), 0, 0)
        }
        column.addView(mark)
        column.addView(by)
        return column
    }

    private fun showProgress(text: String) = page(icon(), body(text).apply { gravity = Gravity.CENTER })

    private fun showUnreachable(pairing: Pairing, tried: List<String>) {
        val views = mutableListOf<View>(
            icon(),
            title("Can't reach ${pairing.name}"),
            body("Tried ${tried.joinToString(" and ")} (port ${pairing.port}).\n\n" +
                "• The computer must be on, with Routine Panel's service running.\n" +
                "• At home, this phone must be on the home Wi-Fi.\n" +
                "• Away from home, Tailscale must be on, on this phone and on the computer."),
            button("Try again") { connect() },
        )
        packageManager.getLaunchIntentForPackage("com.tailscale.ipn")?.let { open ->
            views.add(button("Open Tailscale") { startActivity(open) })
        }
        views.add(button("Scan a new code") { scan() })
        page(*views.toTypedArray())
    }

    private fun showRemoved(pairing: Pairing) = page(
        icon(),
        title("This phone was removed"),
        body("${pairing.name} no longer knows this phone: it was removed there, or its pairing code was never used " +
            "in time. To see the routines again, pair again: on the computer, click Phone, then Pair a phone."),
        button("Scan a new code") { scan() },
        button("Forget this computer") { forget() },
    )

    private fun showMessage(text: String) {
        AlertDialog.Builder(this).setMessage(text).setPositiveButton("OK") { _, _ -> if (web == null) start() }.show()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
}
