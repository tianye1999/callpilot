pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // LiveKit Android 的 audioswitch 依赖发布在 JitPack；范围限定到 com.github.*
        maven(url = "https://jitpack.io") {
            content { includeGroupByRegex("""com\.github\..*""") }
        }
    }
}

rootProject.name = "callpilot-android"
include(":app")
