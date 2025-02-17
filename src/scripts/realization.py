from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_timestamp, from_json, from_unixtime, lit, round, struct, to_json, unix_timestamp
from pyspark.sql.types import LongType, StructType, StructField, StringType, TimestampType

TOPIC_IN = 'student.topic.cohort25.helendrug_in'
TOPIC_OUT = 'student.topic.cohort25.helendrug_out'


# метод для записи данных в 2 target: в PostgreSQL для фидбэков и в Kafka для триггеров
def foreach_batch_function(df, epoch_id):
    # сохраняем df в памяти, чтобы не создавать df заново перед отправкой в Kafka
    df.persist()
    # записываем df в PostgreSQL с полем feedback
    df_pg = df.withColumn('feedback', lit(None).cast(StringType()))
    (df_pg.write
        .format('jdbc')
        .option('url', 'jdbc:postgresql://localhost:5432/de')
        .option('driver', 'org.postgresql.Driver')
        .option('schema', 'public')
        .option('dbtable', 'subscribers_feedback')
        .option('user', 'jovyan')
        .option('password', 'jovyan')
        .option('autoCommit', 'true')
        .mode('append')
        .save())
    # создаём df для отправки в Kafka. Сериализация в json.
    df_kafka = df.select(
        to_json(struct(col('*'))).alias('value')
    ).select('value')
    # отправляем сообщения в результирующий топик Kafka без поля feedback
    (df_kafka.write
        .format('kafka')
        .option('kafka.bootstrap.servers', 'rc1b-2erh7b35n4j4v869.mdb.yandexcloud.net:9091')
        .option('kafka.security.protocol', 'SASL_SSL')
        .option('kafka.sasl.mechanism', 'SCRAM-SHA-512')
        .option('kafka.sasl.jaas.config',
                'org.apache.kafka.common.security.scram.ScramLoginModule required username=\"de-student\" password=\"ltcneltyn\";')
        .option('topic', TOPIC_OUT)
        .option('truncate', False)
        .save())

    # очищаем память от df
    df.unpersist()


# необходимые библиотеки для интеграции Spark с Kafka и PostgreSQL
spark_jars_packages = ','.join(
        [
            'org.apache.spark:spark-sql-kafka-0-10_2.12:3.3.0',
            'org.postgresql:postgresql:42.4.0',
        ]
    )

# создаём spark сессию с необходимыми библиотеками в spark_jars_packages для интеграции с Kafka и PostgreSQL
spark = (SparkSession.builder
         .appName('RestaurantSubscribeStreamingService')
         .config('spark.sql.session.timeZone', 'UTC')
         .config('spark.jars.packages', spark_jars_packages)
         .getOrCreate())

# читаем из топика Kafka сообщения с акциями от ресторанов
restaurant_read_stream_df = (spark.readStream
                             .format('kafka')
                             .option('kafka.bootstrap.servers', 'rc1b-2erh7b35n4j4v869.mdb.yandexcloud.net:9091')
                             .option('kafka.security.protocol', 'SASL_SSL')
                             .option('kafka.sasl.jaas.config', 'org.apache.kafka.common.security.scram.ScramLoginModule required username="login" password="password";')
                             .option('kafka.sasl.mechanism', 'SCRAM-SHA-512')
                             .option('subscribe', TOPIC_IN)
                             .load())

# определяем схему входного сообщения для json
incomming_message_schema = StructType([
    StructField('restaurant_id', StringType(), True),
    StructField('adv_campaign_id', StringType(), True),
    StructField('adv_campaign_content', StringType(), True),
    StructField('adv_campaign_owner', StringType(), True),
    StructField('adv_campaign_owner_contact', StringType(), True),
    StructField('adv_campaign_datetime_start', LongType(), True),
    StructField('adv_campaign_datetime_end', LongType(), True),
    StructField('datetime_created', LongType(), True)
])

# десериализуем из value сообщения json и фильтруем по времени старта и окончания акции
filtered_read_stream_df = (
    restaurant_read_stream_df
    .withColumn('value', col('value').cast(StringType()))
    .withColumn('event', from_json(col('value'), incomming_message_schema))
    .selectExpr('event.*')
    .filter(lit(unix_timestamp(current_timestamp())).between(
        col('adv_campaign_datetime_start'), col('adv_campaign_datetime_end')
    ))
)

# вычитываем всех пользователей с подпиской на рестораны
subscribers_restaurant_df = (
    spark.read
    .format('jdbc')
    .option('url', 'jdbc:postgresql://rc1a-fswjkpli01zafgjm.mdb.yandexcloud.net:6432/de')
    .option('driver', 'org.postgresql.Driver')
    .option('dbtable', 'subscribers_restaurants')
    .option('user', 'student')
    .option('password', 'de-student')
    .load()
)

# джойним данные из сообщения Kafka с пользователями подписки по restaurant_id (uuid). Добавляем время создания события.
result_df = (
    filtered_read_stream_df.join(subscribers_restaurant_df, on='restaurant_id', how='inner')
    .withColumn('datetime_created',
                from_unixtime(col('datetime_created'), "yyyy-MM-dd' 'HH:mm:ss.SSS").cast(TimestampType()))
    .dropDuplicates(['restaurant_id', 'adv_campaign_id', 'adv_campaign_datetime_start'])
    .withWatermark('datetime_created', '10 minutes')
    .withColumn('trigger_datetime_created', lit(round(unix_timestamp(current_timestamp()))).cast(LongType()))
)

# запускаем стриминг
result_df.writeStream \
    .foreachBatch(foreach_batch_function) \
    .start() \
    .awaitTermination()
