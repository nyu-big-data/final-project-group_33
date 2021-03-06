import getpass

from pyspark.sql import SparkSession,Window
from pyspark.ml.evaluation import RegressionEvaluator,RankingEvaluator
from pyspark.ml.recommendation import ALS
from pyspark.ml.tuning import ParamGridBuilder, CrossValidator
from pyspark.sql.functions import col,rank
import pyspark.sql.functions as func

import pandas as pd
import time

def train_model(spark, netID,size_type, latentRanks, regularizationParams):
    schema = 'userId INT, movieId INT, rating FLOAT , timestamp INT, title STRING'
    ratingsTrain = spark.read.parquet(f'hdfs:/user/{netID}/movielens/{size_type}/train.parquet' ,header=True ,schema=schema)
    # ratingsTrain.drop('timestamp')
    
    ratingsTrain = ratingsTrain.withColumn('userId', col('userId').cast('integer')).withColumn('movieId', col('movieId').cast('integer')).withColumn('rating', col('rating').cast('float')).drop('timestamp')
    
    # Build the recommendation model using ALS on the training data
    # Note we set cold start strategy to 'drop' to ensure we don't get NaN evaluation metrics
    als = ALS(maxIter=5, userCol="userId", itemCol="movieId", ratingCol="rating", nonnegative = True, implicitPrefs = False, coldStartStrategy="drop")
   
    # Add hyperparameters and their respective values to param_grid
    param_grid = ParamGridBuilder().addGrid(als.rank, latentRanks).addGrid(als.regParam, regularizationParams).build()

    # Define evaluator as RMSE 
    evaluator = RegressionEvaluator(metricName="rmse", labelCol="rating", predictionCol="prediction") 
    
    # Build cross validation using CrossValidator
    cv = CrossValidator(estimator=als, estimatorParamMaps=param_grid, evaluator=evaluator, numFolds=5,parallelism=2)

    #Fit cross validator to the training dataset
    model = cv.fit(ratingsTrain)
    #fetch the best model
    best_model = model.bestModel
    
    print("Best Model - Rank:",best_model._java_obj.parent().getRank(), " RegParam:",best_model._java_obj.parent().getRegParam())
    # best_model.save("./models")
    
    return best_model,evaluator



def evaluate_test_pred(model,evaluator):
    schema = 'userId INT, movieId INT, rating FLOAT , timestamp INT, title STRING'
    ratingsTest = spark.read.parquet(f'hdfs:/user/{netID}/movielens/{size_type}/test.parquet' ,header=True,schema=schema)
    ratingsTest = ratingsTest.withColumn('userId', col('userId').cast('integer')).withColumn('movieId', col('movieId').cast('integer')).withColumn('rating', col('rating').cast('float')).drop('timestamp')
    
    test_pred = model.transform(ratingsTest)
    
    # RMSE Metrics
    rmse = evaluator.evaluate(test_pred)
    print("rmse",rmse)

    # Ranking Evaluator (MAP -> Mean Average Precision)
    window = Window.partitionBy(test_pred['userId']).orderBy(test_pred['prediction'].desc())  
    test_pred = test_pred.withColumn('rank', rank().over(window)).filter(col('rank') <= 100).groupby("userId").agg(func.collect_list(test_pred['movieId'].cast('double')).alias('pred_movies'))
    
    window = Window.partitionBy(ratingsTest['userId']).orderBy(ratingsTest['rating'].desc())  
    df_mov = ratingsTest.withColumn('rank', rank().over(window)).filter(col('rank') <= 100).groupby("userId").agg(func.collect_list(ratingsTest['movieId'].cast('double')).alias('movies'))
    
    test_pred = test_pred.join(df_mov, test_pred.userId==df_mov.userId).drop('userId')
    
    metrics = ['meanAveragePrecision','meanAveragePrecisionAtK','precisionAtK','ndcgAtK','recallAtK']
    metricsDict = {
        'rmse':rmse
    }
    for metric in metrics:
        rEvaluator = RankingEvaluator(predictionCol='pred_movies', labelCol='movies', metricName=metric)
        metricsDict[metric] = rEvaluator.evaluate(test_pred)
        
    print(metricsDict)

    return metricsDict,test_pred



def superitems(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            for i in superitems(v):
                yield (k,) + i
    else:
        yield (obj,)


def nested_dict_to_csv(data,columns,model_name):
    df = pd.DataFrame([*superitems(data)],columns=columns)
    return df.to_csv("./{}.csv".format(model_name))

if __name__ == "__main__":
    spark = SparkSession.builder.appName("Recommender-Model-GRP33").getOrCreate()

    regularizationParams = [.01, .05, .1, .2]
    latentRanks = [10, 50, 100, 150]
    netID = getpass.getuser()
    
    size_types = ['ml-latest','ml-latest-small']
    metrics = {}
    for size_type in size_types:
        start_time = time.time()
        model,evaluator = train_model(spark, netID,size_type, latentRanks, regularizationParams)

        metricDict,_ = evaluate_test_pred(model,evaluator)
        metrics[size_type] = metricDict
        print("ALS Dataset Size: {}, Time: {} seconds".format(size_type, (time.time() - start_time)))

    print(nested_dict_to_csv(metrics,['Dataset','Metric Name','Value'],'ALS'))

    spark.stop()