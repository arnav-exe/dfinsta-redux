.class public Lcom/dfinstagram/dialog_maker;
.super Ljava/lang/Object;


# instance fields
.field private IGDSDialog:LX/6Gp;


# direct methods
.method public constructor <init>(Landroid/content/Context;)V
    .locals 1

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    new-instance v0, LX/6Gp;

    invoke-direct {v0, p1}, LX/6Gp;-><init>(Landroid/content/Context;)V

    iput-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    return-void
.end method


# virtual methods
.method public addDialogMenuItems([Ljava/lang/CharSequence;Landroid/content/DialogInterface$OnClickListener;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p2, p1}, LX/6Gp;->A0S(Landroid/content/DialogInterface$OnClickListener;[Ljava/lang/CharSequence;)V

    return-void
.end method

.method public getDialog()Landroid/app/Dialog;
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0}, LX/6Gp;->A05()Landroid/app/Dialog;

    move-result-object v0

    return-object v0
.end method

.method public setBlueButton(Ljava/lang/String;Landroid/content/DialogInterface$OnClickListener;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p2, p1}, LX/6Gp;->A0R(Landroid/content/DialogInterface$OnClickListener;Ljava/lang/String;)V

    return-void
.end method

.method public setCancelable(Z)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p1}, LX/6Gp;->A0g(Z)V

    return-void
.end method

.method public setCanceledOnTouchOutside(Z)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p1}, LX/6Gp;->A0h(Z)V

    return-void
.end method

.method public setMessage(Ljava/lang/CharSequence;)V
    .locals 2

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p1}, LX/6Gp;->A0f(Ljava/lang/CharSequence;)V

    return-void
.end method

.method public setNegativeButton(Ljava/lang/String;Landroid/content/DialogInterface$OnClickListener;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p2, p1}, LX/6Gp;->A0Q(Landroid/content/DialogInterface$OnClickListener;Ljava/lang/String;)V

    return-void
.end method

.method public setOnDismissListener(Landroid/content/DialogInterface$OnDismissListener;)V
    .locals 2

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p1}, LX/6Gp;->A0T(Landroid/content/DialogInterface$OnDismissListener;)V

    return-void
.end method

.method public setPositiveButton(Ljava/lang/String;Landroid/content/DialogInterface$OnClickListener;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    invoke-virtual {v0, p2, p1}, LX/6Gp;->A0P(Landroid/content/DialogInterface$OnClickListener;Ljava/lang/String;)V

    return-void
.end method

.method public setTitle(Ljava/lang/String;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/dialog_maker;->IGDSDialog:LX/6Gp;

    iput-object p1, v0, LX/6Gp;->A03:Ljava/lang/String;

    return-void
.end method
