import ccxt
import csv
import numpy as np
import os
import pandas as pd
import time
import threading
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import List

from pdr_backend.predictoor.examples.models.pdr_model_simple.model import OceanModel
from pdr_backend.predictoor.examples.models.predict import predict_function
from pdr_backend.utils.subgraph import get_all_interesting_prediction_contracts
from pdr_backend.utils.contract import PredictoorContract, Web3Config
from pdr_backend.utils import env


last_block_time = 0
topics: List[dict] = []

rpc_url = env.get_rpc_url_or_exit()
subgraph_url = env.get_subgraph_or_exit()
private_key = env.get_private_key_or_exit()
pair_filters = env.get_pair_filter()
timeframe_filter = env.get_timeframe_filter()
source_filter = env.get_source_filter()
owner_addresses = env.get_owner_addresses()

web3_config = Web3Config(rpc_url, private_key)
owner = web3_config.owner

exchange_id = "binance"
pair = "BTC/TUSD"
timeframe = "5m"
exchange_class = getattr(ccxt, exchange_id)
exchange_ccxt = exchange_class({"timeout": 30000})

models = [
    OceanModel(exchange_id, pair, timeframe),
]


def process_block(block, model, main_pd):
    global topics
    """ Process each contract and if needed, get a prediction, submit it and claim revenue for past epoch """
    if not topics:
        topics = get_all_interesting_prediction_contracts(
            subgraph_url,
            pair_filters,
            timeframe_filter,
            source_filter,
            owner_addresses,
        )
    print(f"Got new block: {block['number']} with {len(topics)} topics")
    for address in topics:
        topic = topics[address]
        predictoor_contract = PredictoorContract(web3_config, address)
        epoch = predictoor_contract.get_current_epoch()
        seconds_per_epoch = predictoor_contract.get_secondsPerEpoch()
        seconds_till_epoch_end = (
            epoch * seconds_per_epoch + seconds_per_epoch - block["timestamp"]
        )
        print(
            f"\t{topic['name']} (at address {topic['address']} is at epoch {epoch}, seconds_per_epoch: {seconds_per_epoch}, seconds_till_epoch_end: {seconds_till_epoch_end}"
        )

        if epoch > topic["last_submited_epoch"] and topic["last_submited_epoch"] > 0:
            # let's get the payout for previous epoch.  We don't care if it fails...
            slot = epoch * seconds_per_epoch - seconds_per_epoch
            print(
                f"Contract:{predictoor_contract.contract_address} - Claiming revenue for slot:{slot}"
            )
            predictoor_contract.payout(slot, False)

        if seconds_till_epoch_end <= int(os.getenv("SECONDS_TILL_EPOCH_END", 60)):
            """Timestamp of prediction"""
            target_time = (epoch + 2) * seconds_per_epoch

            """Let's fetch the prediction """
            (predicted_value, predicted_confidence) = predict_function(
                topic, target_time, model, main_pd
            )
            if predicted_value is not None and predicted_confidence > 0:
                """We have a prediction, let's submit it"""
                stake_amount = (
                    os.getenv("STAKE_AMOUNT", 1) * predicted_confidence / 100
                )  # TODO have a customizable function to handle this
                print(
                    f"Contract:{predictoor_contract.contract_address} - Submiting prediction for slot:{target_time}"
                )
                predictoor_contract.submit_prediction(
                    predicted_value, stake_amount, target_time, True
                )
                topics[address]["last_submited_epoch"] = epoch
                return predicted_value
            else:
                print(
                    f"We do not submit, prediction function returned ({predicted_value}, {predicted_confidence})"
                )


def log_loop(blockno, model, main_pd):
    global last_block_time
    block = web3_config.w3.eth.get_block(blockno, full_transactions=False)
    if block:
        last_block_time = block["timestamp"]
        prediction = process_block(block, model, main_pd)
        if prediction is not None:
            return prediction


def main():
    print("Starting main loop...")

    ts_now = int(time.time())

    results_path = "results"
    if not os.path.exists(results_path):
        os.makedirs(results_path)

    results_csv_name = (
        "./"
        + results_path
        + "/"
        + exchange_id
        + "_"
        + models[0].pair
        + "_"
        + models[0].timeframe
        + "_"
        + str(ts_now)
        + ".csv"
    )

    columns_short = ["datetime", "open", "high", "low", "close", "volume"]

    columns_models = []
    for model in models:
        model.unpickle_model("./pdr_backend/predictoor/examples/models")
        columns_models.append(model.model_name)  # prediction column.  0 or 1

    all_columns = columns_short + columns_models

    # write csv header for results
    size = 0
    try:
        files_stats = os.stat(results_csv_name)
        size = files_stats.st_size
    except:
        pass
    if size == 0:
        with open(results_csv_name, "a") as f:
            writer = csv.writer(f)
            writer.writerow(all_columns)

    # read initial set of candles
    candles = exchange_ccxt.fetch_ohlcv(pair, "5m")
    # load past data
    main_pd = pd.DataFrame(columns=all_columns)
    for ohl in candles:
        ohlc = {
            "timestamp": int(ohl[0] / 1000),
            "open": float(ohl[1]),
            "close": float(ohl[4]),
            "low": float(ohl[3]),
            "high": float(ohl[2]),
            "volume": float(ohl[5]),
        }
        main_pd.loc[ohlc["timestamp"]] = ohlc
        main_pd["datetime"] = pd.to_datetime(main_pd.index.values, unit="s", utc=True)

    lastblock = 0
    last_finalized_timestamp = 0
    while True:
        candles = exchange_ccxt.fetch_ohlcv(pair, "5m")

        # update last two candles
        for ohl in candles[-2:]:
            t = int(ohl[0] / 1000)
            main_pd.loc[t, ["datetime"]] = pd.to_datetime(t, unit="s", utc=True)
            main_pd.loc[t, ["open"]] = float(ohl[1])
            main_pd.loc[t, ["close"]] = float(ohl[4])
            main_pd.loc[t, ["low"]] = float(ohl[3])
            main_pd.loc[t, ["high"]] = float(ohl[2])
            main_pd.loc[t, ["volume"]] = float(ohl[5])

        timestamp = main_pd.index.values[-2]

        block = web3_config.w3.eth.block_number
        if block > lastblock:
            lastblock = block

            # #we have a new candle
            if last_finalized_timestamp < timestamp:
                last_finalized_timestamp = timestamp

                should_write = False
                for model in models:
                    prediction = main_pd.iloc[-2][model.model_name]
                    if not np.isnan(prediction):
                        should_write = True

                if should_write:
                    with open(results_csv_name, "a") as f:
                        writer = csv.writer(f)
                        row = [
                            main_pd.index.values[-2],
                            main_pd.iloc[-2]["datetime"],
                            main_pd.iloc[-2]["open"],
                            main_pd.iloc[-2]["high"],
                            main_pd.iloc[-2]["low"],
                            main_pd.iloc[-2]["close"],
                            main_pd.iloc[-2]["volume"],
                        ]
                        for model in models:
                            row.append(main_pd.iloc[-2][model.model_name])
                        writer.writerow(row)

            for model in models:
                index = main_pd.index.values[-1]
                current_prediction = main_pd.iloc[-1][model.model_name]
                if np.isnan(current_prediction):
                    prediction = log_loop(
                        block,
                        model,
                        main_pd.drop(columns_models + ["datetime"], axis=1),
                    )
                    if prediction is not None:
                        main_pd.loc[index, [model.model_name]] = float(prediction)

            print(
                main_pd.loc[
                    :, ~main_pd.columns.isin(["volume", "open", "high", "low"])
                ].tail(15)
            )

        else:
            time.sleep(1)


if __name__ == "__main__":
    main()