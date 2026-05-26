.class public Lcom/dfinstagram/hooks;
.super Ljava/lang/Object;


# static fields
.field public static final TAG:Ljava/lang/String; = "instagram"

.field private static bufferStreamField:Ljava/lang/reflect/Field;

.field private static readBufferField:Ljava/lang/reflect/Field;


# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static handleStartActivity(Landroid/app/Activity;Landroid/content/Intent;)Landroid/content/Intent;
    .locals 0

    return-object p1
.end method

.method public static isModifiableRequest(Lcom/facebook/proxygen/NativeReadBuffer;)Z
    .locals 2

    iget-object v0, p0, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;

    invoke-virtual {v0}, Ljava/net/URI;->getHost()Ljava/lang/String;

    move-result-object v0

    const-string v1, "i.instagram.com"

    invoke-virtual {v0, v1}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;

    invoke-virtual {v0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/api/v1/feed/timeline/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-eqz v0, :cond_1

    const-string v1, "disable_suggested_posts"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_1

    :cond_0
    const/4 v0, 0x1

    :goto_0
    return v0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public static jniHandlerSendHeaders(Lcom/facebook/proxygen/JniHandler;Lorg/apache/http/client/methods/HttpUriRequest;)V
    .locals 2
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const/4 v1, 0x0

    const/4 v0, 0x0

    invoke-static {p0, p1, v0, v1, v1}, Lcom/dfinstagram/hooks;->jniHandlerSendRequest(Lcom/facebook/proxygen/JniHandler;Lorg/apache/http/client/methods/HttpUriRequest;[BII)V

    return-void
.end method

.method public static jniHandlerSendRequest(Lcom/facebook/proxygen/JniHandler;Lorg/apache/http/client/methods/HttpUriRequest;[BII)V
    .locals 14
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const/4 v6, 0x0

    const/4 v5, 0x0

    :try_start_0
    invoke-virtual {p1}, Ljava/lang/Object;->getClass()Ljava/lang/Class;

    move-result-object v9

    const-string v10, "getURI"

    const/4 v11, 0x0

    new-array v11, v11, [Ljava/lang/Class;

    invoke-virtual {v9, v10, v11}, Ljava/lang/Class;->getMethod(Ljava/lang/String;[Ljava/lang/Class;)Ljava/lang/reflect/Method;

    move-result-object v9

    const/4 v10, 0x0

    new-array v10, v10, [Ljava/lang/Object;

    invoke-virtual {v9, p1, v10}, Ljava/lang/reflect/Method;->invoke(Ljava/lang/Object;[Ljava/lang/Object;)Ljava/lang/Object;

    move-result-object v9

    move-object v0, v9

    check-cast v0, Ljava/net/URI;

    move-object v6, v0

    sget-object v9, Lcom/dfinstagram/hooks;->readBufferField:Ljava/lang/reflect/Field;

    if-nez v9, :cond_0

    iget-object v9, p0, Lcom/facebook/proxygen/JniHandler;->mResponseHandler:Lcom/facebook/proxygen/HTTPResponseHandler;

    invoke-virtual {v9}, Ljava/lang/Object;->getClass()Ljava/lang/Class;

    move-result-object v9

    invoke-virtual {v9}, Ljava/lang/Class;->getDeclaredFields()[Ljava/lang/reflect/Field;

    move-result-object v4

    const-string v9, "instagram"

    iget-object v10, p0, Lcom/facebook/proxygen/JniHandler;->mResponseHandler:Lcom/facebook/proxygen/HTTPResponseHandler;

    invoke-virtual {v10}, Ljava/lang/Object;->getClass()Ljava/lang/Class;

    move-result-object v10

    invoke-virtual {v10}, Ljava/lang/Class;->getName()Ljava/lang/String;

    move-result-object v10

    invoke-static {v9, v10}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    iget-object v9, p0, Lcom/facebook/proxygen/JniHandler;->mResponseHandler:Lcom/facebook/proxygen/HTTPResponseHandler;

    invoke-virtual {v9}, Ljava/lang/Object;->getClass()Ljava/lang/Class;

    move-result-object v9

    invoke-virtual {v9}, Ljava/lang/Class;->getSuperclass()Ljava/lang/Class;

    move-result-object v9

    const/4 v10, 0x1

    invoke-static {v9, v10}, Lcom/dfinstagram/hooks;->logSuperclasses(Ljava/lang/Class;I)V

    array-length v10, v4

    const/4 v9, 0x0

    :goto_0
    if-ge v9, v10, :cond_0

    aget-object v3, v4, v9

    const-string v11, "instagram"

    new-instance v12, Ljava/lang/StringBuilder;

    invoke-direct {v12}, Ljava/lang/StringBuilder;-><init>()V

    invoke-virtual {v3}, Ljava/lang/reflect/Field;->toString()Ljava/lang/String;

    move-result-object v13

    invoke-virtual {v12, v13}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v12

    const-string v13, " = "

    invoke-virtual {v12, v13}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v12

    iget-object v13, p0, Lcom/facebook/proxygen/JniHandler;->mResponseHandler:Lcom/facebook/proxygen/HTTPResponseHandler;

    invoke-virtual {v3, v13}, Ljava/lang/reflect/Field;->get(Ljava/lang/Object;)Ljava/lang/Object;

    move-result-object v13

    invoke-virtual {v12, v13}, Ljava/lang/StringBuilder;->append(Ljava/lang/Object;)Ljava/lang/StringBuilder;

    move-result-object v12

    invoke-virtual {v12}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v12

    invoke-static {v11, v12}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    invoke-virtual {v3}, Ljava/lang/reflect/Field;->getType()Ljava/lang/Class;

    move-result-object v11

    invoke-virtual {v11}, Ljava/lang/Class;->getName()Ljava/lang/String;

    move-result-object v11

    const-string v12, "com.facebook.proxygen.ReadBuffer"

    invoke-virtual {v11, v12}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v11

    if-eqz v11, :cond_6

    const/4 v9, 0x1

    invoke-virtual {v3, v9}, Ljava/lang/reflect/Field;->setAccessible(Z)V

    sput-object v3, Lcom/dfinstagram/hooks;->readBufferField:Ljava/lang/reflect/Field;

    :cond_0
    sget-object v9, Lcom/dfinstagram/hooks;->readBufferField:Ljava/lang/reflect/Field;

    iget-object v10, p0, Lcom/facebook/proxygen/JniHandler;->mResponseHandler:Lcom/facebook/proxygen/HTTPResponseHandler;

    invoke-virtual {v9, v10}, Ljava/lang/reflect/Field;->get(Ljava/lang/Object;)Ljava/lang/Object;

    move-result-object v1

    check-cast v1, Lcom/facebook/proxygen/NativeReadBuffer;

    iput-object v6, v1, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;
    :try_end_0
    .catch Ljava/lang/Exception; {:try_start_0 .. :try_end_0} :catch_0

    :goto_1

    invoke-static {v6}, Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V

    return-void

    :cond_6
    add-int/lit8 v9, v9, 0x1

    goto/16 :goto_0

    :catch_0
    move-exception v7

    const-string v9, "instagram"

    invoke-static {v9, v7}, Landroid/util/Log;->w(Ljava/lang/String;Ljava/lang/Throwable;)I

    goto :goto_1

.end method

.method private static logSuperclasses(Ljava/lang/Class;I)V
    .locals 6

    if-nez p0, :cond_0

    :goto_0
    return-void

    :cond_0
    const-string v0, ""

    const/4 v1, 0x0

    :goto_1
    if-ge v1, p1, :cond_1

    new-instance v3, Ljava/lang/StringBuilder;

    invoke-direct {v3}, Ljava/lang/StringBuilder;-><init>()V

    invoke-virtual {v3, v0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v3

    const-string v4, "-"

    invoke-virtual {v3, v4}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v3

    invoke-virtual {v3}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v0

    add-int/lit8 v1, v1, 0x1

    goto :goto_1

    :cond_1
    const-string v3, "instagram"

    new-instance v4, Ljava/lang/StringBuilder;

    invoke-direct {v4}, Ljava/lang/StringBuilder;-><init>()V

    invoke-virtual {v4, v0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v4

    const-string v5, " "

    invoke-virtual {v4, v5}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v4

    invoke-virtual {p0}, Ljava/lang/Class;->getName()Ljava/lang/String;

    move-result-object v5

    invoke-virtual {v4, v5}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v4

    invoke-virtual {v4}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v4

    invoke-static {v3, v4}, Landroid/util/Log;->v(Ljava/lang/String;Ljava/lang/String;)I

    invoke-virtual {p0}, Ljava/lang/Class;->getSuperclass()Ljava/lang/Class;

    move-result-object v2

    add-int/lit8 v3, p1, 0x1

    invoke-static {v2, v3}, Lcom/dfinstagram/hooks;->logSuperclasses(Ljava/lang/Class;I)V

    goto :goto_0
.end method

.method public static maybeReadAndModifyResponse(Lcom/facebook/proxygen/NativeReadBuffer;)V
    .locals 12
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const/16 v11, 0x1000

    const/4 v10, 0x0

    iget-object v7, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    if-nez v7, :cond_4

    new-instance v2, Ljava/io/ByteArrayOutputStream;

    invoke-direct {v2}, Ljava/io/ByteArrayOutputStream;-><init>()V

    new-array v0, v11, [B

    :goto_0
    invoke-virtual {p0}, Lcom/facebook/proxygen/NativeReadBuffer;->_size()I

    move-result v5

    :cond_0
    :goto_1
    if-lez v5, :cond_2

    invoke-virtual {p0, v0, v10, v11}, Lcom/facebook/proxygen/NativeReadBuffer;->_read([BII)I

    move-result v3

    sub-int/2addr v5, v3

    const-string v7, "instagram"

    new-instance v8, Ljava/lang/StringBuilder;

    invoke-direct {v8}, Ljava/lang/StringBuilder;-><init>()V

    const-string v9, "reading original response: "

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8, v3}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v8

    const-string v9, ", "

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8, v5}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v8

    const-string v9, " left"

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v8

    invoke-static {v7, v8}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    if-lez v3, :cond_1

    invoke-virtual {v2, v0, v10, v3}, Ljava/io/ByteArrayOutputStream;->write([BII)V

    :cond_1
    if-nez v5, :cond_0

    invoke-virtual {p0}, Lcom/facebook/proxygen/NativeReadBuffer;->_size()I

    move-result v5

    const-string v7, "instagram"

    new-instance v8, Ljava/lang/StringBuilder;

    invoke-direct {v8}, Ljava/lang/StringBuilder;-><init>()V

    const-string v9, "read done, next size is "

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8, v5}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v8

    invoke-static {v7, v8}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    goto :goto_1

    :cond_2
    const-string v7, "instagram"

    new-instance v8, Ljava/lang/StringBuilder;

    invoke-direct {v8}, Ljava/lang/StringBuilder;-><init>()V

    const-string v9, "read: DONE "

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v8

    iget-object v9, p0, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;

    invoke-virtual {v8, v9}, Ljava/lang/StringBuilder;->append(Ljava/lang/Object;)Ljava/lang/StringBuilder;

    move-result-object v8

    invoke-virtual {v8}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v8

    invoke-static {v7, v8}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    new-instance v4, Ljava/lang/String;

    invoke-virtual {v2}, Ljava/io/ByteArrayOutputStream;->toByteArray()[B

    move-result-object v7

    const-string v8, "UTF-8"

    invoke-direct {v4, v7, v8}, Ljava/lang/String;-><init>([BLjava/lang/String;)V

    const-string v7, "}"

    invoke-virtual {v4, v7}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v7

    if-eqz v7, :cond_5

    :try_start_0
    new-instance v1, Lorg/json/JSONObject;

    invoke-direct {v1, v4}, Lorg/json/JSONObject;-><init>(Ljava/lang/String;)V
    :try_end_0
    .catch Ljava/lang/Exception; {:try_start_0 .. :try_end_0} :catch_1

    :try_start_1
    iget-object v7, p0, Lcom/facebook/proxygen/NativeReadBuffer;->requestURI:Ljava/net/URI;

    invoke-virtual {v7}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v7

    const-string v8, "/api/v1/feed/timeline/"

    invoke-virtual {v7, v8}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v7

    if-eqz v7, :cond_3

    invoke-static {v1}, Lcom/dfinstagram/hooks;->modifyFeedResponse(Lorg/json/JSONObject;)[B

    move-result-object v7

    iput-object v7, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    :cond_3
    :goto_2
    const/4 v7, 0x0

    iput v7, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I
    :try_end_1
    .catch Ljava/lang/Exception; {:try_start_1 .. :try_end_1} :catch_2

    :cond_4
    :goto_3
    return-void

    :cond_5
    const-string v7, "instagram"

    const-string v8, "Incomplete response, waiting for more data"

    invoke-static {v7, v8}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    const-wide/16 v8, 0x32

    :try_start_2
    invoke-static {v8, v9}, Ljava/lang/Thread;->sleep(J)V
    :try_end_2
    .catch Ljava/lang/Exception; {:try_start_2 .. :try_end_2} :catch_0

    goto/16 :goto_0

    :catch_0
    move-exception v7

    goto/16 :goto_0

    :catch_1
    move-exception v6

    const-string v7, "instagram"

    const-string v8, "still incomplete response"

    invoke-static {v7, v8, v6}, Landroid/util/Log;->w(Ljava/lang/String;Ljava/lang/String;Ljava/lang/Throwable;)I

    goto :goto_3

    :catch_2
    move-exception v6

    const-string v7, "instagram"

    const-string v8, "error modifying response"

    invoke-static {v7, v8, v6}, Landroid/util/Log;->e(Ljava/lang/String;Ljava/lang/String;Ljava/lang/Throwable;)I

    invoke-virtual {v2}, Ljava/io/ByteArrayOutputStream;->toByteArray()[B

    move-result-object v7

    iput-object v7, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    iput v10, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    goto :goto_3
.end method

.method private static modifyFeedResponse(Lorg/json/JSONObject;)[B
    .locals 12
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Lorg/json/JSONException;,
            Ljava/io/UnsupportedEncodingException;
        }
    .end annotation

    const-string v7, "pagination_source"

    invoke-virtual {p0, v7}, Lorg/json/JSONObject;->has(Ljava/lang/String;)Z

    move-result v1

    if-eqz v1, : cond_isnt_feed_recs

    invoke-virtual {p0, v7}, Lorg/json/JSONObject;->getString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v1
    .local v1, "paginationSource":Ljava/lang/String;

    const-string v7, "feed_recs"

    invoke-virtual {v1, v7}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v1

    if-eqz v1, : cond_isnt_feed_recs

    new-instance v6, Lorg/json/JSONArray;

    invoke-direct {v6}, Lorg/json/JSONArray;-><init>()V

    const-string v7, "feed_items"

    invoke-virtual {p0, v7, v6}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    goto: cond_end

    :cond_isnt_feed_recs
    # ================================================================================================================ #
    const/4 v11, 0x0

    const-string v7, "feed_items"

    invoke-virtual {p0, v7}, Lorg/json/JSONObject;->optJSONArray(Ljava/lang/String;)Lorg/json/JSONArray;

    move-result-object v1
    .local v1, "feedItems":Lorg/json/JSONArray;

    if-eqz v1, :cond_end

    new-instance v6, Lorg/json/JSONArray;

    invoke-direct {v6}, Lorg/json/JSONArray;-><init>()V

    const/4 v3, 0x0

    :goto_0
    invoke-virtual {v1}, Lorg/json/JSONArray;->length()I

    move-result v7

    if-ge v3, v7, :cond_add_new_array_to_old_json_object

    invoke-virtual {v1, v3}, Lorg/json/JSONArray;->getJSONObject(I)Lorg/json/JSONObject;

    move-result-object v5
    .local v5, "currentFeedItem":Lorg/json/JSONObject;

    const-string v7, "end_of_feed_demarcator"

    invoke-virtual {v5, v7}, Lorg/json/JSONObject;->optJSONObject(Ljava/lang/String;)Lorg/json/JSONObject;

    move-result-object v4

    if-eqz v4, :cond_iterate_again

    const-string v7, "groups"

    invoke-virtual {v4, v7}, Lorg/json/JSONObject;->has(Ljava/lang/String;)Z

    move-result v7

    if-eqz v7, :cond_iterate_again

    invoke-static {v5}, Lcom/dfinstagram/hooks;->modifyEndOfFeedDemarcator(Lorg/json/JSONObject;)Lorg/json/JSONObject;

    move-result-object v5

    :cond_iterate_again

    invoke-virtual {v6, v5}, Lorg/json/JSONArray;->put(Ljava/lang/Object;)Lorg/json/JSONArray;
    add-int/lit8 v3, v3, 0x1

    goto: goto_0

    :cond_add_new_array_to_old_json_object
    const-string v7, "feed_items"

    invoke-virtual {p0, v7, v6}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    # ================================================================================================================ #
    :cond_end
    invoke-virtual {p0}, Lorg/json/JSONObject;->toString()Ljava/lang/String;

    move-result-object v7

    const-string v8, "UTF-8"

    invoke-virtual {v7, v8}, Ljava/lang/String;->getBytes(Ljava/lang/String;)[B

    move-result-object v7

    return-object v7

.end method


.method private static modifyEndOfFeedDemarcator(Lorg/json/JSONObject;)Lorg/json/JSONObject;
    .locals 8

    new-instance v7, Lorg/json/JSONArray;

    invoke-direct {v7}, Lorg/json/JSONArray;-><init>()V

    const-string v4, "groups"

    invoke-virtual {p0, v4}, Lorg/json/JSONObject;->optJSONArray(Ljava/lang/String;)Lorg/json/JSONArray;

    move-result-object v0

    if-eqz v0, :cond_end

    const/4 v1, 0x0

    invoke-virtual {v0}, Lorg/json/JSONArray;->length()I

    move-result v2
    .local v2, "arrayLength":I
    :goto_loop_start

    if-ge v1, v2, :cond_add_new_array

    invoke-virtual {v0, v1}, Lorg/json/JSONArray;->getJSONObject(I)Lorg/json/JSONObject;

    move-result-object v3

    const-string v4, "id"

    invoke-virtual {v3, v4}, Lorg/json/JSONObject;->getString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v4

    const-string v6, "feed_recs"

    invoke-virtual {v4, v6}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v6

    if-nez v6, : cond_increment

    invoke-virtual {v7, v3}, Lorg/json/JSONArray;->put(Ljava/lang/Object;)Lorg/json/JSONArray;

    :cond_increment
    add-int/lit8 v1, v1, 0x1

    goto: goto_loop_start

    :cond_add_new_array

    const-string v6, "groups"

    invoke-virtual {p0, v6, v7}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    :cond_end
    return-object p0
.end method


.method public static nativeReadBufferRead(Lcom/facebook/proxygen/NativeReadBuffer;[BII)I
    .locals 5
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    invoke-static {p0}, Lcom/dfinstagram/hooks;->isModifiableRequest(Lcom/facebook/proxygen/NativeReadBuffer;)Z

    move-result v2

    if-eqz v2, :cond_1

    invoke-static {p0}, Lcom/dfinstagram/hooks;->maybeReadAndModifyResponse(Lcom/facebook/proxygen/NativeReadBuffer;)V

    iget-object v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    if-eqz v2, :cond_1

    iget v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    iget-object v3, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    array-length v3, v3

    if-lt v2, v3, :cond_0

    const-string v2, "instagram"

    const-string v3, "modified response eof"

    invoke-static {v2, v3}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    const/4 v0, 0x0

    :goto_0
    return v0

    :cond_0
    const-string v2, "instagram"

    new-instance v3, Ljava/lang/StringBuilder;

    invoke-direct {v3}, Ljava/lang/StringBuilder;-><init>()V

    const-string v4, "serving modified response: "

    invoke-virtual {v3, v4}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v3

    iget v4, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    invoke-virtual {v3, v4}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v3

    const-string v4, "/"

    invoke-virtual {v3, v4}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v3

    iget-object v4, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    array-length v4, v4

    invoke-virtual {v3, v4}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v3

    invoke-virtual {v3}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v3

    invoke-static {v2, v3}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    iget-object v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    array-length v2, v2

    iget v3, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    sub-int/2addr v2, v3

    invoke-static {p3, v2}, Ljava/lang/Math;->min(II)I

    move-result v0

    iget-object v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    iget v3, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    invoke-static {v2, v3, p1, p2, v0}, Ljava/lang/System;->arraycopy(Ljava/lang/Object;ILjava/lang/Object;II)V

    iget v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    add-int/2addr v2, v0

    iput v2, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    goto :goto_0

    :cond_1
    invoke-virtual {p0, p1, p2, p3}, Lcom/facebook/proxygen/NativeReadBuffer;->_read([BII)I

    move-result v1

    move v0, v1

    goto :goto_0
.end method

.method public static nativeReadBufferSize(Lcom/facebook/proxygen/NativeReadBuffer;)I
    .locals 6

    invoke-static {p0}, Lcom/dfinstagram/hooks;->isModifiableRequest(Lcom/facebook/proxygen/NativeReadBuffer;)Z

    move-result v3

    if-eqz v3, :cond_0

    :try_start_0
    invoke-static {p0}, Lcom/dfinstagram/hooks;->maybeReadAndModifyResponse(Lcom/facebook/proxygen/NativeReadBuffer;)V
    :try_end_0
    .catch Ljava/io/IOException; {:try_start_0 .. :try_end_0} :catch_0

    :cond_0
    :goto_0
    iget-object v3, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    if-eqz v3, :cond_1

    iget-object v3, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponse:[B

    array-length v3, v3

    iget v4, p0, Lcom/facebook/proxygen/NativeReadBuffer;->modifiedResponseOffset:I

    sub-int v0, v3, v4

    const-string v3, "instagram"

    new-instance v4, Ljava/lang/StringBuilder;

    invoke-direct {v4}, Ljava/lang/StringBuilder;-><init>()V

    const-string v5, "returning size for modified response: "

    invoke-virtual {v4, v5}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v4

    invoke-virtual {v4, v0}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    move-result-object v4

    invoke-virtual {v4}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v4

    invoke-static {v3, v4}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I

    :goto_1
    return v0

    :catch_0
    move-exception v2

    const-string v3, "instagram"

    invoke-static {v3, v2}, Landroid/util/Log;->w(Ljava/lang/String;Ljava/lang/Throwable;)I

    goto :goto_0

    :cond_1
    invoke-virtual {p0}, Lcom/facebook/proxygen/NativeReadBuffer;->_size()I

    move-result v1

    move v0, v1

    goto :goto_1
.end method


.method public static throwIfBlocked(Ljava/net/URI;)V

    .locals 4

   invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/feed/timeline/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-eqz v3, :cond_11

    const-string v0, "disable_feed"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_8

    :cond_11
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/discover/topical_explore"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_12

    const-string v0, "disable_explore"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_8

    :cond_12
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/qp/batch_fetch/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-nez v3, :cond_reels

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/api/v1/clips/home/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-nez v3, :cond_reels

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/clips/discover"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_13


    :cond_reels
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_8

    :cond_13
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/feed/reels_tray/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-eqz v3, :cond_14

    const-string v0, "disable_stories"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_8

    :cond_14
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "minishop"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_return_void

    const-string v0, "disable_shopping"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_8

    :cond_return_void
    return-void

    :cond_8

    new-instance v2, Ljava/io/IOException;

    const-string v0, "URL has no host"

    invoke-direct {v2, v0}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V

    throw v2
.end method


.method public static modifyTigonBuffer(Ljava/nio/ByteBuffer;)Ljava/nio/ByteBuffer;

    .locals 13
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Ljava/io/IOException;
        }
    .end annotation

    const/16 v3, 0x1000

    const/4 v4, 0x0

    invoke-static {v3}, Ljava/nio/ByteBuffer;->allocate(I)Ljava/nio/ByteBuffer;

    move-result-object v2

    new-instance v5, Ljava/io/ByteArrayOutputStream;

    invoke-direct {v5}, Ljava/io/ByteArrayOutputStream;-><init>()V

    const/4 v11, 0x0

    :goto_0
    invoke-virtual {p0}, Ljava/nio/Buffer;->hasRemaining()Z

    move-result v0

    if-eqz v0, :cond_2

    invoke-virtual {p0}, Ljava/nio/Buffer;->remaining()I

    move-result v0

    invoke-static {v0, v3}, Ljava/lang/Math;->min(II)I


    move-result v1

    invoke-virtual {v2, v1}, Ljava/nio/Buffer;->limit(I)Ljava/nio/Buffer;

    invoke-virtual {v2}, Ljava/nio/ByteBuffer;->array()[B

    move-result-object v0

    invoke-virtual {p0, v0, v4, v1}, Ljava/nio/ByteBuffer;->get([BII)Ljava/nio/ByteBuffer;

    # here is where I fill my own buffer
    invoke-virtual {v5, v0, v4, v1}, Ljava/io/ByteArrayOutputStream;->write([BII)V

    invoke-virtual {v2}, Ljava/nio/Buffer;->clear()Ljava/nio/Buffer;

    # End of the loop.
    :cond_2


    new-instance v8, Ljava/lang/String;

    invoke-virtual {v5}, Ljava/io/ByteArrayOutputStream;->toByteArray()[B

    move-result-object v7

    const-string v9, "UTF-8"

    invoke-direct {v8, v7, v9}, Ljava/lang/String;-><init>([BLjava/lang/String;)V

    const-string v7, "}"

    invoke-virtual {v8, v7}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v7

    if-nez v7, :cond_modify_and_return

    const-string v7, "}\n"

    invoke-virtual {v8, v7}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v7

    if-nez v7, :cond_modify_and_return

    add-int/lit8 v11, v11, 0x1

    const v12, 0x14

    if-ge v11, v12, :cond_bail

    const-wide/16 v8, 0x32

    invoke-static {v8, v9}, Ljava/lang/Thread;->sleep(J)V

    goto/16 :goto_0

    # =================================================================================================================

    :cond_modify_and_return
    new-instance v10, Lorg/json/JSONObject;

    invoke-direct {v10, v8}, Lorg/json/JSONObject;-><init>(Ljava/lang/String;)V

    invoke-static {v10}, Lcom/dfinstagram/hooks;->modifyFeedResponse(Lorg/json/JSONObject;)[B

    move-result-object p0

    invoke-static {p0}, Ljava/nio/ByteBuffer;->wrap([B)Ljava/nio/ByteBuffer;

    move-result-object p0

    return-object p0

    :cond_bail

    invoke-virtual {p0}, Ljava/nio/ByteBuffer;->rewind()Ljava/nio/ByteBuffer;

    move-result-object p0

    return-object p0

.end method