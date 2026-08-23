.class public Lcom/dfinstagram/startapp;
.super Ljava/lang/Object;


# static fields
.field public static ctx:Landroid/content/Context;

.field public static feedCacheCoordinator:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;


# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static clearFeedCache()V
    .locals 1

    const/4 v0, 0x0

    sput-object v0, Lcom/dfinstagram/startapp;->feedCacheCoordinator:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    return-void
.end method

.method public static setContext(Landroid/app/Application;)V
    .locals 0

    sput-object p0, Lcom/dfinstagram/startapp;->ctx:Landroid/content/Context;

    return-void
.end method

.method public static setFeedCache(Lcom/instagram/mainfeed/network/FeedCacheCoordinator;)V
    .locals 0

    sput-object p0, Lcom/dfinstagram/startapp;->feedCacheCoordinator:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    return-void
.end method
