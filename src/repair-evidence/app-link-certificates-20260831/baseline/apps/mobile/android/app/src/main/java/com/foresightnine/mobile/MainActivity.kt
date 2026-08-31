package com.foresightnine.mobile

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle

import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

import expo.modules.ReactActivityDelegateWrapper

class MainActivity : ReactActivity() {
  companion object {
    // This is a routing bound, not an evidence acceptance limit.  The JavaScript
    // evidence normalizer remains the sole place that validates URLs and creates
    // an EvidenceBundle.  Keeping the intent payload bounded prevents a shared
    // document from becoming an oversized deep-link query string.
    private const val MAX_SHARED_TEXT_LENGTH = 8_192
  }

  /**
   * Android shares deliver text through ACTION_SEND rather than a URI that Expo
   * Router can consume. Convert only the two declared text MIME types into the
   * existing app deep link. No URL is opened or fetched at this boundary.
   */
  private fun routeIncomingShare(intent: Intent) {
    val mimeType = intent.type?.lowercase()
    if (
      intent.action != Intent.ACTION_SEND ||
      (mimeType != "text/plain" && mimeType != "text/uri-list")
    ) return

    val sharedText = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty()
    val route = Uri.Builder()
      .scheme("foresightnine")
      .authority("evidence")

    if (sharedText.isBlank()) {
      route.appendQueryParameter("share_error", "empty")
    } else if (sharedText.length > MAX_SHARED_TEXT_LENGTH) {
      route.appendQueryParameter("share_error", "too_long")
    } else {
      route.appendQueryParameter("shared", sharedText)
    }

    intent.action = Intent.ACTION_VIEW
    intent.type = null
    intent.data = route.build()
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    // Set the theme to AppTheme BEFORE onCreate to support
    // coloring the background, status bar, and navigation bar.
    // This is required for expo-splash-screen.
    setTheme(R.style.AppTheme);
    routeIncomingShare(intent)
    super.onCreate(null)
  }

  override fun onNewIntent(intent: Intent) {
    routeIncomingShare(intent)
    super.onNewIntent(intent)
    setIntent(intent)
  }

  /**
   * Returns the name of the main component registered from JavaScript. This is used to schedule
   * rendering of the component.
   */
  override fun getMainComponentName(): String = "main"

  /**
   * Returns the instance of the [ReactActivityDelegate]. We use [DefaultReactActivityDelegate]
   * which allows you to enable New Architecture with a single boolean flags [fabricEnabled]
   */
  override fun createReactActivityDelegate(): ReactActivityDelegate {
    return ReactActivityDelegateWrapper(
          this,
          BuildConfig.IS_NEW_ARCHITECTURE_ENABLED,
          object : DefaultReactActivityDelegate(
              this,
              mainComponentName,
              fabricEnabled
          ){})
  }

  /**
    * Align the back button behavior with Android S
    * where moving root activities to background instead of finishing activities.
    * @see <a href="https://developer.android.com/reference/android/app/Activity#onBackPressed()">onBackPressed</a>
    */
  override fun invokeDefaultOnBackPressed() {
      if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.R) {
          if (!moveTaskToBack(false)) {
              // For non-root activities, use the default implementation to finish them.
              super.invokeDefaultOnBackPressed()
          }
          return
      }

      // Use the default back button implementation on Android S
      // because it's doing more than [Activity.moveTaskToBack] in fact.
      super.invokeDefaultOnBackPressed()
  }
}
