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

.method public static startDfInstagramSettings()V
    .locals 3

    sget-object v0, Lcom/dfinstagram/startapp;->ctx:Landroid/content/Context;

    if-eqz v0, :cond_return

    new-instance v1, Landroid/content/Intent;

    const-class v2, Lcom/dfinstagram/preference/Preference;

    invoke-direct {v1, v0, v2}, Landroid/content/Intent;-><init>(Landroid/content/Context;Ljava/lang/Class;)V

    const/high16 v2, 0x10000000

    invoke-virtual {v1, v2}, Landroid/content/Intent;->addFlags(I)Landroid/content/Intent;

    invoke-virtual {v0, v1}, Landroid/content/Context;->startActivity(Landroid/content/Intent;)V

    :cond_return
    return-void
.end method
