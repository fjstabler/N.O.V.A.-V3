plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.nova.panel"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.nova.panel"
        // An Echo Show 5 running LineageOS is Android 11 (API 30) or later.
        // 26 is the floor because that is where adaptive launcher icons and
        // notification channels begin — going lower would mean carrying a
        // second icon pipeline for devices this will never run on.
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "3.0.0"
    }

    buildTypes {
        release {
            // Nothing here is worth obfuscating, and a stack trace from a
            // device on a wall is hard enough to get without renaming.
            isMinifyEnabled = false
            // Signed with the debug key so `assembleRelease` produces something
            // installable without a keystore. This is a personal appliance on a
            // private network, not something going near a store.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // The WebSocket. Writing one by hand would be a week of someone else's
    // bugs; OkHttp's is small, well-tested and already on most devices' minds.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}
