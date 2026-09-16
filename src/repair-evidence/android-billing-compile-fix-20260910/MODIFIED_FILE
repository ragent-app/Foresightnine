package com.foresightnine.mobile

import android.app.Activity
import com.android.billingclient.api.BillingClient
import com.android.billingclient.api.BillingClientStateListener
import com.android.billingclient.api.BillingClient.ProductType.SUBS
import com.android.billingclient.api.BillingFlowParams
import com.android.billingclient.api.ProductDetails
import com.android.billingclient.api.Purchase
import com.android.billingclient.api.PurchasesUpdatedListener
import com.android.billingclient.api.PendingPurchasesParams
import com.android.billingclient.api.QueryProductDetailsParams
import com.android.billingclient.api.QueryPurchasesParams
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.ReadableArray
import com.facebook.react.bridge.WritableArray
import com.facebook.react.bridge.WritableMap
import com.facebook.react.modules.core.DeviceEventManagerModule

/**
 * Minimal native bridge for Play subscriptions. It exposes product metadata
 * and purchase tokens only to the JS layer in memory; the server verifies the
 * token before an entitlement is granted.
 */
class ForesightNineBillingModule(
  private val reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext), PurchasesUpdatedListener {
  private var billingClient: BillingClient? = null
  private val productsById = mutableMapOf<String, ProductDetails>()
  private val basePlanByProductId = mutableMapOf<String, String>()

  override fun getName(): String = "ForesightNineBilling"

  @ReactMethod
  fun addListener(@Suppress("UNUSED_PARAMETER") eventName: String) {
    // React Native requires these bookkeeping methods for NativeEventEmitter.
  }

  @ReactMethod
  fun removeListeners(@Suppress("UNUSED_PARAMETER") count: Int) {
    // Events are delivered while the module is alive; no native subscription is held.
  }

  private fun client(): BillingClient {
    return billingClient ?: BillingClient.newBuilder(reactContext)
      .setListener(this)
      .enablePendingPurchases(PendingPurchasesParams.newBuilder().build())
      .enableAutoServiceReconnection()
      .build()
      .also { billingClient = it }
  }

  private fun billingError(result: com.android.billingclient.api.BillingResult, promise: Promise) {
    promise.reject("BILLING_${result.responseCode}", result.debugMessage)
  }

  private fun ensureReady(promise: Promise, action: (BillingClient) -> Unit) {
    val current = client()
    if (current.isReady) {
      action(current)
      return
    }
    current.startConnection(object : BillingClientStateListener {
      override fun onBillingSetupFinished(result: com.android.billingclient.api.BillingResult) {
        if (result.responseCode == BillingClient.BillingResponseCode.OK) action(current)
        else billingError(result, promise)
      }

      override fun onBillingServiceDisconnected() {
        promise.reject("BILLING_DISCONNECTED", "Google Play Billing is disconnected.")
      }
    })
  }

  @ReactMethod
  fun queryProducts(productIds: ReadableArray, promise: Promise) {
    val ids = (0 until productIds.size()).mapNotNull { index -> productIds.getString(index)?.takeIf { it.isNotBlank() } }.distinct()
    if (ids.isEmpty()) {
      promise.resolve(Arguments.createArray())
      return
    }
    ensureReady(promise) { current ->
      val products = ids.map { id -> QueryProductDetailsParams.Product.newBuilder().setProductId(id).setProductType(SUBS).build() }
      current.queryProductDetailsAsync(QueryProductDetailsParams.newBuilder().setProductList(products).build()) { result, details ->
        if (result.responseCode != BillingClient.BillingResponseCode.OK) {
          billingError(result, promise)
          return@queryProductDetailsAsync
        }
        productsById.clear()
        basePlanByProductId.clear()
        val output = Arguments.createArray()
        details.productDetailsList.forEach { product ->
          val offer = product.subscriptionOfferDetails?.firstOrNull { it.basePlanId == "monthly" }
            ?: product.subscriptionOfferDetails?.firstOrNull()
          if (offer != null) {
            productsById[product.productId] = product
            basePlanByProductId[product.productId] = offer.basePlanId
            output.pushMap(productMap(product, offer))
          }
        }
        promise.resolve(output)
      }
    }
  }

  @ReactMethod
  fun launchPurchase(productId: String, offerToken: String, obfuscatedAccountId: String, promise: Promise) {
    if (productId.isBlank() || offerToken.isBlank() || obfuscatedAccountId.isBlank()) {
      promise.reject("BILLING_INVALID_INPUT", "A product, offer, and account binding are required.")
      return
    }
    val activity: Activity = reactContext.currentActivity ?: run {
      promise.reject("BILLING_NO_ACTIVITY", "The billing activity is unavailable.")
      return
    }
    ensureReady(promise) { current ->
      val product = productsById[productId]
      if (product == null) {
        promise.reject("BILLING_PRODUCT_NOT_LOADED", "Query the subscription before starting purchase.")
        return@ensureReady
      }
      val item = BillingFlowParams.ProductDetailsParams.newBuilder()
        .setProductDetails(product)
        .setOfferToken(offerToken)
        .build()
      val flow = BillingFlowParams.newBuilder()
        .setProductDetailsParamsList(listOf(item))
        .setObfuscatedAccountId(obfuscatedAccountId)
        .build()
      val result = current.launchBillingFlow(activity, flow)
      if (result.responseCode != BillingClient.BillingResponseCode.OK) billingError(result, promise)
      else promise.resolve(true)
    }
  }

  @ReactMethod
  fun restorePurchases(promise: Promise) {
    ensureReady(promise) { current ->
      current.queryPurchasesAsync(QueryPurchasesParams.newBuilder().setProductType(SUBS).build()) { result, purchases ->
        if (result.responseCode != BillingClient.BillingResponseCode.OK) {
          billingError(result, promise)
          return@queryPurchasesAsync
        }
        val output = Arguments.createArray()
        purchases.forEach { output.pushMap(purchaseMap(it)) }
        promise.resolve(output)
      }
    }
  }

  override fun onPurchasesUpdated(result: com.android.billingclient.api.BillingResult, purchases: MutableList<Purchase>?) {
    if (result.responseCode != BillingClient.BillingResponseCode.OK || purchases == null) {
      val event = Arguments.createMap()
      event.putInt("responseCode", result.responseCode)
      event.putString("debugMessage", result.debugMessage)
      emit("foresightnineBillingPurchaseUpdated", event)
      return
    }
    purchases.forEach { emit("foresightnineBillingPurchaseUpdated", purchaseMap(it)) }
  }

  private fun productMap(product: ProductDetails, offer: ProductDetails.SubscriptionOfferDetails): WritableMap {
    val price = offer.pricingPhases.pricingPhaseList.firstOrNull()
    return Arguments.createMap().apply {
      putString("productId", product.productId)
      putString("basePlanId", offer.basePlanId)
      putString("offerToken", offer.offerToken)
      putString("formattedPrice", price?.formattedPrice)
      putString("currencyCode", price?.priceCurrencyCode)
      putDouble("priceAmountMicros", price?.priceAmountMicros?.toDouble() ?: 0.0)
      putString("billingPeriod", price?.billingPeriod)
    }
  }

  private fun purchaseMap(purchase: Purchase): WritableMap {
    val productId = purchase.products.firstOrNull()
    return Arguments.createMap().apply {
      putString("purchaseToken", purchase.purchaseToken)
      putString("productId", productId)
      putString("basePlanId", productId?.let { basePlanByProductId[it] })
      putInt("purchaseState", purchase.purchaseState)
      putBoolean("isAcknowledged", purchase.isAcknowledged)
      putBoolean("isAutoRenewing", purchase.isAutoRenewing)
    }
  }

  private fun emit(eventName: String, payload: WritableMap) {
    reactContext.getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java).emit(eventName, payload)
  }
}
