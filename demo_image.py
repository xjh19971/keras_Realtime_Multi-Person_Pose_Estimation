import os
import sys
import argparse
import cv2
import math
import time
import numpy as np
import util
from config_reader import config_reader
from scipy.ndimage.filters import gaussian_filter
import tensorflow.contrib.tensorrt as trt
from model.cmu_model_mobilenet import get_testing_model
import tensorflow as tf
from keras import backend as K
# find connection in the specified sequence, center 29 is in the position 15
limbSeq = [[1,2],[2,3]]

# the middle joints heatmap correpondence
mapIdx = [[2,3],[4,5]]

# visualize
colors = [[255, 0, 0], [0, 255, 0],[0, 0, 255]]

def freeze_session(session, keep_var_names=None, output_names=None, clear_devices=True,input_names=None):
    """
    Freezes the state of a session into a pruned computation graph.

    Creates a new computation graph where variable nodes are replaced by
    constants taking their current value in the session. The new graph will be
    pruned so subgraphs that are not necessary to compute the requested
    outputs are removed.
    @param session The TensorFlow session to be frozen.
    @param keep_var_names A list of variable names that should not be frozen,
                          or None to freeze all the variables in the graph.
    @param output_names Names of the relevant graph outputs.
    @param clear_devices Remove the device directives from the graph for better portability.
    @return The frozen graph definition.
    """
    from tensorflow.python.framework.graph_util import convert_variables_to_constants
    graph = session.graph
    with graph.as_default():
        freeze_var_names = list(set(v.op.name for v in tf.global_variables()).difference(keep_var_names or []))
        output_names = output_names or []
        output_names += [v.op.name for v in tf.global_variables()]
        # Graph -> GraphDef ProtoBuf
        input_graph_def = graph.as_graph_def()
        if clear_devices:
            for node in input_graph_def.node:
                node.device = ""
        frozen_graph = convert_variables_to_constants(session, input_graph_def,
                                                      output_names, freeze_var_names)
        return frozen_graph,output_names,input_names

def process (input_image, params, model_params):

    oriImg = cv2.imread(input_image)  # B,G,R order
    multiplier = [x * model_params['boxsize'] / oriImg.shape[0] for x in params['scale_search']]

    heatmap_avg = np.zeros((oriImg.shape[0], oriImg.shape[1], 4))
    paf_avg = np.zeros((oriImg.shape[0], oriImg.shape[1], 4))

    K.set_learning_phase(0)
    # load model

    # authors of original model don't use
    # vgg normalization (subtracting mean) on input images
    model = get_testing_model()
    model.load_weights(keras_weights_file)
    frozen_graph,output_names,input_names = freeze_session(K.get_session(),
                                  output_names=[out.op.name for out in model.outputs],
                                  input_names=[out.op.name for out in model.inputs])
    #tf.train.write_graph(frozen_graph, ".", "tf_model.pb", as_text=False)

    trt_graph = trt.create_inference_graph(
        input_graph_def=frozen_graph,
        outputs=output_names,
        max_batch_size=1,
        max_workspace_size_bytes=1 << 25,
        precision_mode='FP16',
        minimum_segment_size=50
    )

    #with open('trt_model.pb', 'wb') as f:
    #    f.write(trt_graph.SerializeToString())
    tf_config = tf.ConfigProto()
    tf_config.gpu_options.allow_growth = True
    tf_sess = tf.Session(config=tf_config)
    tf.import_graph_def(trt_graph, name='')
    tf_input = tf_sess.graph.get_tensor_by_name(input_names[0] + ':0')
    tf_paf = tf_sess.graph.get_tensor_by_name(output_names[0]+':0')
    tf_heatmap = tf_sess.graph.get_tensor_by_name(output_names[1]+':0')
    for m in range(len(multiplier)):
        scale = multiplier[m]

        imageToTest = cv2.resize(oriImg, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        imageToTest_padded, pad = util.padRightDownCorner(imageToTest, model_params['stride'],
                                                          model_params['padValue'])

        input_img = np.transpose(np.float32(imageToTest_padded[:,:,:,np.newaxis]), (3,0,1,2)) # required shape (1, width, height, channels)
        tf_paf, tf_heatmap = tf_sess.run([tf_paf, tf_heatmap],
                                            feed_dict={
                                                tf_input: input_image[None, ...]
                                                             })
        tf_sess.close()
        output_blobs=[tf_paf,tf_heatmap]
        # extract outputs, resize, and remove padding
        heatmap = np.squeeze(output_blobs[1])  # output 1 is heatmaps
        heatmap = cv2.resize(heatmap, (0, 0), fx=model_params['stride'], fy=model_params['stride'],
                             interpolation=cv2.INTER_CUBIC)
        heatmap = heatmap[:imageToTest_padded.shape[0] - pad[2], :imageToTest_padded.shape[1] - pad[3],
                  :]
        heatmap = cv2.resize(heatmap, (oriImg.shape[1], oriImg.shape[0]), interpolation=cv2.INTER_CUBIC)

        paf = np.squeeze(output_blobs[0])  # output 0 is PAFs
        paf = cv2.resize(paf, (0, 0), fx=model_params['stride'], fy=model_params['stride'],
                         interpolation=cv2.INTER_CUBIC)
        paf = paf[:imageToTest_padded.shape[0] - pad[2], :imageToTest_padded.shape[1] - pad[3], :]
        paf = cv2.resize(paf, (oriImg.shape[1], oriImg.shape[0]), interpolation=cv2.INTER_CUBIC)

        heatmap_avg = heatmap_avg + heatmap / len(multiplier)
        paf_avg = paf_avg + paf / len(multiplier)

    all_peaks = []
    peak_counter = 0

    for part in [0,1,2]:
        map_ori = heatmap_avg[:, :, part]
        map = gaussian_filter(map_ori, sigma=3)

        map_left = np.zeros(map.shape)
        map_left[1:, :] = map[:-1, :]
        map_right = np.zeros(map.shape)
        map_right[:-1, :] = map[1:, :]
        map_up = np.zeros(map.shape)
        map_up[:, 1:] = map[:, :-1]
        map_down = np.zeros(map.shape)
        map_down[:, :-1] = map[:, 1:]

        peaks_binary = np.logical_and.reduce(
            (map >= map_left, map >= map_right, map >= map_up, map >= map_down, map > params['thre1']))
        peaks = list(zip(np.nonzero(peaks_binary)[1], np.nonzero(peaks_binary)[0]))  # note reverse
        peaks_with_score = [x + (map_ori[x[1], x[0]],) for x in peaks]
        id = range(peak_counter, peak_counter + len(peaks))
        peaks_with_score_and_id = [peaks_with_score[i] + (id[i],) for i in range(len(id))]

        all_peaks.append(peaks_with_score_and_id)
        peak_counter += len(peaks)

    connection_all = []
    special_k = []
    mid_num = 10
    limit=[[60,10],[80,10]]
    for k in range(len(mapIdx)):
        score_mid = paf_avg[:, :, [x - 2 for x in mapIdx[k]]]
        candA = all_peaks[limbSeq[k][0] - 1]
        candB = all_peaks[limbSeq[k][1] - 1]
        nA = len(candA)
        nB = len(candB)
        candlist=[]
        if (nA != 0 and nB != 0):
            for i in range(nA):
                tempcand=[]
                for j in range(nB):
                    vecx1 = candA[i][0]-candB[j][0]
                    vecy1 = candA[i][1]-candB[j][1]
                    disx = abs(vecx1)
                    disy = abs(vecy1)
                    r = math.sqrt(disx * disx + disy * disy)
                    if(r<limit[k][0] and r>limit[k][1]):
                        tempcand.append(j)
                candlist.append(tempcand)
            indexA, indexB = limbSeq[k]
            connection_candidate = []
            for i in range(len(candlist)):
                for j in candlist[i]:
                    vec = np.subtract(candB[j][:2], candA[i][:2])
                    norm = math.sqrt(vec[0] * vec[0] + vec[1] * vec[1])
                    # failure case when 2 body parts overlaps
                    if norm == 0:
                        continue
                    vec = np.divide(vec, norm)

                    startend = list(zip(np.linspace(candA[i][0], candB[j][0], num=mid_num), \
                                   np.linspace(candA[i][1], candB[j][1], num=mid_num)))

                    vec_x = np.array(
                        [score_mid[int(round(startend[I][1])), int(round(startend[I][0])), 0] \
                         for I in range(len(startend))])
                    vec_y = np.array(
                        [score_mid[int(round(startend[I][1])), int(round(startend[I][0])), 1] \
                         for I in range(len(startend))])

                    score_midpts = np.multiply(vec_x, vec[0]) + np.multiply(vec_y, vec[1])
                    score_with_dist_prior = sum(score_midpts) / len(score_midpts) + min(
                        0.5 * oriImg.shape[0] / norm - 1, 0)
                    criterion1 = len(np.nonzero(score_midpts > params['thre2'])[0]) > 0.8 * len(
                        score_midpts)
                    criterion2 = score_with_dist_prior > 0
                    if criterion1 and criterion2:
                        connection_candidate.append([i, j, score_with_dist_prior,
                                                     score_with_dist_prior + candA[i][2] + candB[j][2]])

            connection_candidate = sorted(connection_candidate, key=lambda x: x[2], reverse=True)
            connection = np.zeros((0, 5))
            for c in range(len(connection_candidate)):
                i, j, s = connection_candidate[c][0:3]
                if (i not in connection[:, 3] and j not in connection[:, 4]):
                    connection = np.vstack([connection, [candA[i][3], candB[j][3], s, i, j]])
                    if (len(connection) >= min(nA, nB)):
                        break

            connection_all.append(connection)
        else:
            special_k.append(k)
            connection_all.append([])

    # last number in each row is the total parts number of that person
    # the second last number in each row is the score of the overall configuration
    subset = -1 * np.ones((0, 5))
    candidate = np.array([item for sublist in all_peaks for item in sublist])

    for k in range(len(mapIdx)):
        if k not in special_k:
            partAs = connection_all[k][:, 0]
            partBs = connection_all[k][:, 1]
            indexA, indexB = np.array(limbSeq[k]) - 1

            for i in range(len(connection_all[k])):  # = 1:size(temp,1)
                found = 0
                subset_idx = [-1, -1]
                for j in range(len(subset)):  # 1:size(subset,1):
                    if subset[j][indexA] == partAs[i] or subset[j][indexB] == partBs[i]:
                        subset_idx[found] = j
                        found += 1

                if found == 1:
                    j = subset_idx[0]
                    if (subset[j][indexB] != partBs[i]):
                        subset[j][indexB] = partBs[i]
                        subset[j][-1] += 1
                        subset[j][-2] += candidate[partBs[i].astype(int), 2] + connection_all[k][i][2]
                elif found == 2:  # if found 2 and disjoint, merge them
                    j1, j2 = subset_idx
                    membership = ((subset[j1] >= 0).astype(int) + (subset[j2] >= 0).astype(int))[:-2]
                    if len(np.nonzero(membership == 2)[0]) == 0:  # merge
                        subset[j1][:-2] += (subset[j2][:-2] + 1)
                        subset[j1][-2:] += subset[j2][-2:]
                        subset[j1][-2] += connection_all[k][i][2]
                        subset = np.delete(subset, j2, 0)
                    else:  # as like found == 1
                        subset[j1][indexB] = partBs[i]
                        subset[j1][-1] += 1
                        subset[j1][-2] += candidate[partBs[i].astype(int), 2] + connection_all[k][i][2]

                # if find no partA in the subset, create a new subset
                elif not found and k < 2:
                    row = -1 * np.ones(5)
                    row[indexA] = partAs[i]
                    row[indexB] = partBs[i]
                    row[-1] = 2
                    row[-2] = sum(candidate[connection_all[k][i, :2].astype(int), 2]) + \
                              connection_all[k][i][2]
                    subset = np.vstack([subset, row])

    # delete some rows of subset which has few parts occur
    deleteIdx = [];
    for i in range(len(subset)):
        if subset[i][-1] < 2 or subset[i][-2] / subset[i][-1] < 0.2:
            deleteIdx.append(i)
    subset = np.delete(subset, deleteIdx, axis=0)

    canvas = cv2.imread(input_image)  # B,G,R order
    '''for i in [0,1,2]:
        for j in range(len(all_peaks[i])):
            cv2.circle(canvas, all_peaks[i][j][0:2], 4, colors[i], thickness=-1)'''
    for i in [0,1,2]:
        for n in range(len(subset)):
            idx=int(subset[n][i])
            if int(subset[n][i])!=-1:
                cv2.circle(canvas, all_peaks[i][idx-all_peaks[i][0][3]][0:2], 4, colors[i], thickness=-1)

    stickwidth = 4

    for i in range(2):
        for n in range(len(subset)):
            index = subset[n][np.array(limbSeq[i]) - 1]
            if -1 in index:
                continue
            cur_canvas = canvas.copy()
            Y = candidate[index.astype(int), 0]
            X = candidate[index.astype(int), 1]
            mX = np.mean(X)
            mY = np.mean(Y)
            length = ((X[0] - X[1]) ** 2 + (Y[0] - Y[1]) ** 2) ** 0.5
            angle = math.degrees(math.atan2(X[0] - X[1], Y[0] - Y[1]))
            polygon = cv2.ellipse2Poly((int(mY), int(mX)), (int(length / 2), stickwidth), int(angle), 0,
                                       360, 1)
            cv2.fillConvexPoly(cur_canvas, polygon, colors[i])
            canvas = cv2.addWeighted(canvas, 0.4, cur_canvas, 0.6, 0)

    return canvas


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', type=str, required=True, help='input image')
    parser.add_argument('--output', type=str, default='result.png', help='output image')
    parser.add_argument('--model', type=str, default='model/keras/model.h5', help='path to the weights file')

    args = parser.parse_args()
    input_image = args.image
    output = args.output
    keras_weights_file = args.model


    print('start processing...')

    # load model

    # authors of original model don't use
    # vgg normalization (subtracting mean) on input images
    #model = get_testing_model()
    #model.load_weights(keras_weights_file)

    # load config
    params, model_params = config_reader()

    # generate image with body parts
    tic = time.time()
    canvas = process(input_image, params, model_params)

    toc = time.time()
    print ('processing time is %.5f' % (toc - tic))

    cv2.imwrite(output, canvas)

    cv2.destroyAllWindows()



