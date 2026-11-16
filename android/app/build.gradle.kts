import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The release key lives outside the project (~/.android-keys/), so it never travels with the files. Without it the
// release build is signed with the debug key: fine for trying, but it does not update an installed release.
val keyFile = File(System.getProperty("user.home"), ".android-keys/routinepanel-keystore.properties")
val key = Properties().apply {
    if (keyFile.exists()) keyFile.inputStream().use { load(it) }
}

android {
    namespace = "store.themechanic.routinepanel"
    compileSdk = 36
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "store.themechanic.routinepanel"
        minSdk = 30
        targetSdk = 36
        versionCode = 3
        versionName = "0.2.0"
    }

    signingConfigs {
        create("release") {
            if (key.getProperty("storeFile") != null) {
                storeFile = File(key.getProperty("storeFile"))
                storePassword = key.getProperty("storePassword")
                keyAlias = key.getProperty("keyAlias")
                keyPassword = key.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keyFile.exists()) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    androidResources {
        localeFilters += listOf("en")
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    // Google's code scanner: the scanning screen comes from Play services, so the app needs no camera permission.
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
    // Newer versions of what the scanner brings (activity 1.3.1, core 1.2.0, fragment 1.0.0 …): the same ones this
    // computer's build cache already holds, so the app builds without fetching anything.
    implementation("androidx.activity:activity:1.9.3")
    implementation("androidx.fragment:fragment:1.8.5")
    implementation("androidx.core:core:1.15.0")
}
