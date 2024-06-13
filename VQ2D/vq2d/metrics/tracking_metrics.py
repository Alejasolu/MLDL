from collections import OrderedDict
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from ..structures import ResponseTrack
from .utils import PRINT_FORMAT
from .utils import spatio_temporal_iou, spatio_temporal_iou_matches


EPS = 1e-10


class TrackingMetrics(object):

    metric_uuid: str = "Tracking"
    iou_thresholds: np.ndarray = np.array([0.5, 0.75, 0.95])
    ious_to_report: List[float] = [0.5]

    def __init__(
        self,
        ground_truth: List[ResponseTrack],
        prediction: List[List[ResponseTrack]],
        mode: str = "take_max_score",
        ignore_iou_averaging: bool = False,
    ):
        self.ap = None
        self.ground_truth = self._import_ground_truth(ground_truth)
        self.prediction = self._import_prediction(prediction)
        assert mode in ["take_max_stiou", "take_max_score"]
        self.mode = mode
        if mode == "take_max_stiou":
            self.suffix = "(max iou)"
        elif mode == "take_max_score":
            self.suffix = "(max scr)"
        self.ignore_iou_averaging = ignore_iou_averaging

    def _import_ground_truth(self, ground_truth: List[ResponseTrack]) -> pd.DataFrame:
        """Converts input ground-truth to appropriate format."""
        video_lst, response_track_lst = [], []
        for i, gt in enumerate(ground_truth):
            video_lst.append(i)
            response_track_lst.append(gt)

        ground_truth = pd.DataFrame(
            {
                "video-id": video_lst,
                "response_track": response_track_lst,
            }
        )
        return ground_truth

    def _import_prediction(self, prediction: List[List[ResponseTrack]]) -> pd.DataFrame:
        """Converts input predictions to appropriate format."""
        video_lst, response_track_lst, score_lst = [], [], []
        for i, preds in enumerate(prediction):
            # Iterate over each prediction
            for pred in preds:
                score = pred.score
                video_lst.append(i)
                response_track_lst.append(pred)
                score_lst.append(score)
        prediction = pd.DataFrame(
            {
                "video-id": video_lst,
                "response_track": response_track_lst,
                "score": score_lst,
            }
        )
        return prediction

    def evaluate(self) -> None:
        """Evaluates a prediction file. For the detection task we measure the
        interpolated average precision to measure the performance of a
        method.
        """
        self.tracking_metrics = compute_tracking_metrics(
            self.ground_truth, self.prediction, self.iou_thresholds, mode=self.mode
        )
        self.average_tracking_metrics = {
            k: v.mean().item() for k, v in self.tracking_metrics.items()
        }

    def get_metrics(self) -> Dict[str, float]:
        self.evaluate()
        metrics = OrderedDict()
        avg_suffix = f"@ IoU={self.iou_thresholds[0]:.2f}:{self.iou_thresholds[-1]:.2f}"
        for k, v in self.average_tracking_metrics.items():
            metric_name = "{} {} {}".format(self.metric_uuid, k, self.suffix)
            if not self.ignore_iou_averaging:
                metrics[PRINT_FORMAT.format(metric_name, avg_suffix)] = v
            v = self.tracking_metrics[k]
            for tidx, iou_thr in enumerate(self.iou_thresholds):
                if iou_thr not in self.ious_to_report:
                    continue
                metrics[PRINT_FORMAT.format(metric_name, f"@ IoU={iou_thr:.2f}")] = v[
                    tidx
                ].item()
        return metrics


def compute_tracking_metrics(
    ground_truth: pd.DataFrame,
    prediction: pd.DataFrame,
    iou_thresholds: Sequence[float] = np.linspace(0.5, 0.95, 10),
    mode: str = "take_max_stiou",
) -> Dict[str, np.ndarray]:
    """Compute % frames from the ground truth that were accurately recovered
    by the predictions. If multiple predictions occurs for the same
    predicted segment, the one with highest score / stiou is matched as
    true positive. This code is greatly inspired by Pascal VOC devkit.
    Parameters
    ----------
    ground_truth : df
        Data frame containing the ground truth instances.
        Required fields: ['video-id', 'response_track']
    prediction : df
        Data frame containing the prediction instances.
        Required fields: ['video-id, 'response_track', 'score']
    iou_thresholds : 1darray, optional
        Spatio-temporal intersection over union threshold.
    Outputs
    -------
    ap : float
        Average precision score.
    """
    assert mode in ["take_max_stiou", "take_max_score"]
    robustness = np.zeros(len(iou_thresholds))
    if prediction.empty:
        return {"% recovery": robustness}

    # Sort predictions by decreasing score order.
    sort_idx = prediction["score"].values.argsort()[::-1]
    prediction = prediction.loc[sort_idx].reset_index(drop=True)

    # Initialize the iou sum, track sum, track count arrays
    ## st_iou - spatio-temporal IoU between the predicted and ground-truth RTs
    ## iou_sum - sum of IoUs over the accurately tracked bboxes for an RT
    ## track_acc_sum - # of accurately tracked bboxes for an RT
    ## track_total_count - # of total bboxes in the ground-truth RT
    st_iou = np.zeros((len(iou_thresholds), len(prediction)))
    iou_sum = np.zeros((len(iou_thresholds), len(prediction)))
    track_acc_count = np.zeros((len(iou_thresholds), len(prediction)))
    track_total_count = np.zeros((len(iou_thresholds), len(prediction)))

    # Adaptation to query faster
    ground_truth_gbvn = ground_truth.groupby("video-id")

    for idx, this_pred in prediction.iterrows():

        try:
            # Check if there is at least one ground truth in the video associated.
            ground_truth_videoid = ground_truth_gbvn.get_group(this_pred["video-id"])
        except Exception as e:
            # Ignore false positives
            continue

        this_gt = ground_truth_videoid.reset_index()
        # Assuming that there is only 1 ground-truth per video
        stiou_arr = spatio_temporal_iou(
            this_pred["response_track"], this_gt["response_track"].values
        )[0]
        stiou_matches = spatio_temporal_iou_matches(
            this_pred["response_track"], this_gt["response_track"].values
        )[0]

        # Get accuracy and count over the "accurately tracked" frames
        for tidx, iou_thr in enumerate(iou_thresholds):
            stiou_values = np.array(list(stiou_matches.values()))
            mask = stiou_values >= iou_thr
            t_sum = np.count_nonzero(mask)
            i_sum = stiou_values[mask].sum().item()
            st_iou[tidx, idx] = stiou_arr
            iou_sum[tidx, idx] = i_sum
            track_acc_count[tidx, idx] = t_sum
            track_total_count[tidx, idx] = this_gt["response_track"].values[0].length

    # For each ground-truth, pick the prediction with highest ST-IoU or Score
    ## Group predictions by the video-id
    final_iou_sum = [[] for _ in iou_thresholds]
    final_track_acc_count = [[] for _ in iou_thresholds]
    final_track_total_count = [[] for _ in iou_thresholds]
    prediction_gbvn = prediction.groupby("video-id")
    for idx, this_gt in ground_truth.iterrows():
        prediction_videoid = prediction_gbvn.get_group(this_gt["video-id"])
        pred_idxs = prediction_videoid.index.tolist()
        for tidx, iou_thr in enumerate(iou_thresholds):
            # Pick the corresponding max ST-IoU detection.
            if mode == "take_max_stiou":
                max_idx = st_iou[tidx, pred_idxs].argmax().item()
            elif mode == "take_max_score":
                max_idx = prediction_videoid["score"].values.argmax().item()
            max_idx = pred_idxs[max_idx]  # Index into pred_idxs
            final_iou_sum[tidx].append(iou_sum[tidx, max_idx])
            final_track_acc_count[tidx].append(track_acc_count[tidx, max_idx])
            final_track_total_count[tidx].append(track_total_count[tidx, max_idx])

    for tidx, iou_thr in enumerate(iou_thresholds):
        iou_sum = np.array(final_iou_sum[tidx])
        track_acc_count = np.array(final_track_acc_count[tidx])
        track_total_count = np.array(final_track_total_count[tidx])
        if track_total_count.sum() > 0:
            robustness[tidx] = 100.0 * track_acc_count.sum() / (track_total_count.sum())

    return {"% recovery": robustness}
