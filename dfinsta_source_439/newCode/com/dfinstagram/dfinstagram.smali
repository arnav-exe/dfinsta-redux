.class public final Lcom/dfinstagram/dfinstagram;
.super Ljava/lang/Object;

# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static getBoolTrueEz(Ljava/lang/String;)Z
    .locals 3

    sget-object v0, Lcom/dfinstagram/startapp;->ctx:Landroid/content/Context;

    if-nez v0, :cond_context

    const/4 v0, 0x0

    return v0

    :cond_context
    const-string v1, "com.instagram"

    const/4 v2, 0x0

    invoke-virtual {v0, v1, v2}, Landroid/content/Context;->getSharedPreferences(Ljava/lang/String;I)Landroid/content/SharedPreferences;

    move-result-object v0

    const/4 v1, 0x1

    invoke-interface {v0, p0, v1}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v0

    return v0
.end method
