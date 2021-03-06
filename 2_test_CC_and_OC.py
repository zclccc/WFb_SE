import tensorflow as tf
import numpy as np
import sys
import os
import gc
from utils import spectrum_tool
from utils import audio_tool
# from pypesq import pesq
from FLAGS import PARAM
import FLAGS
import math
from dataManager.mixed_aishell_8k_tfrecord_io import generate_tfrecord, get_batch_use_tfdata
import time


def _build_model_use_tfdata(test_set_tfrecords_dir, ckpt_dir):
  '''
  test_set_tfrecords_dir: '/xxx/xxx/*.tfrecords'
  '''
  g = tf.Graph()
  with g.as_default():
    # region TFRecord+DataSet
    with tf.device('/cpu:0'):
      with tf.name_scope('input'):
        x_batch, y_batch, Xtheta_batch, Ytheta_batch, lengths_batch, iter_test = get_batch_use_tfdata(
          test_set_tfrecords_dir,
          get_theta=True)

    with tf.name_scope('model'):
      test_model = PARAM.SE_MODEL(x_batch,
                                  lengths_batch,
                                  y_batch,
                                  Xtheta_batch,
                                  Ytheta_batch,
                                  behavior=PARAM.SE_MODEL.infer)

    init = tf.group(tf.global_variables_initializer(),
                    tf.local_variables_initializer())

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    sess = tf.Session(config=config)
    sess.run(init)

    ckpt = tf.train.get_checkpoint_state(
        os.path.join(PARAM.SAVE_DIR, ckpt_dir))
    test_model.saver.restore(sess, ckpt.model_checkpoint_path)
  g.finalize()
  return sess, test_model, iter_test


def get_PESQ_STOI_SDR(test_set_tfrecords_dir, ckpt_dir, set_name):
  '''
  x_mag : magnitude spectrum of mixed audio.
  x_theta : angle of mixed audio's complex spectrum.
  y_xxx : clean(label) audio's xxx.
  y_xxx_est " estimate audio's xxx.
  '''
  sess, model, iter_test = _build_model_use_tfdata(test_set_tfrecords_dir, ckpt_dir)
  sess.run(iter_test.initializer)
  i = 0
  all_batch = math.ceil(PARAM.DATASET_SIZES[-1] / PARAM.batch_size)
  pesq_mat = None
  stoi_mat = None
  sdr_mat = None
  while True:
    try:
      i += 1
      if i<=all_batch:
        print("-Testing batch %03d/%03d: " % (i,all_batch))
        print('  |-Decoding...')
      time_save = time.time()
      sys.stdout.flush()
      mask, x_mag, x_theta, y_mag, y_theta, y_mag_est, y_theta_est, batch_size = sess.run([model.mask,
                                                                                           model.x_mag,
                                                                                           model.x_theta,
                                                                                           model.y_mag,
                                                                                           model.y_theta,
                                                                                           model.y_mag_estimation,
                                                                                           model.y_theta_estimation,
                                                                                           model.batch_size])
      x_wav = [spectrum_tool.librosa_istft(
          x_mag_t*np.exp(1j*x_theta_t),
          PARAM.NFFT, PARAM.OVERLAP) for x_mag_t, x_theta_t in zip(x_mag, x_theta)]
      y_wav = [spectrum_tool.librosa_istft(
          y_mag_t*np.exp(1j*y_theta_t),
          PARAM.NFFT, PARAM.OVERLAP) for y_mag_t, y_theta_t in zip(y_mag, y_theta)]
      if PARAM.RESTORE_PHASE == 'MIXED':
        y_spec_est = [y_mag_est_t*np.exp(1j*x_theta_t) for y_mag_est_t, x_theta_t in zip(y_mag_est, x_theta)]
        y_wav_est = [spectrum_tool.librosa_istft(y_spec_est_t, PARAM.NFFT, PARAM.OVERLAP) for y_spec_est_t in y_spec_est]
      elif PARAM.RESTORE_PHASE == 'GRIFFIN_LIM':
        y_wav_est = [spectrum_tool.griffin_lim(y_mag_est_t,
                                               PARAM.NFFT,
                                               PARAM.OVERLAP,
                                               PARAM.GRIFFIN_ITERNUM,
                                               x_wav_t) for y_mag_est_t, x_wav_t in zip(y_mag_est, x_wav)]
      elif PARAM.RESTORE_PHASE == 'ESTIMATE':
        if y_theta_est is None:
          print('Model cannot estimate y_theta.')
          exit(-1)
        y_spec_est = [y_mag_est_t*np.exp(1j*y_theta_est_t) for y_mag_est_t, y_theta_est_t in zip(y_mag_est, y_theta_est)]
        y_wav_est = [spectrum_tool.librosa_istft(y_spec_est_t, PARAM.NFFT, PARAM.OVERLAP) for y_spec_est_t in y_spec_est]
      else:
        print('RESTORE_PHASE error.')
        exit(-1)

      # Prevent overflow (else PESQ crashed)
      abs_max = (2 ** (PARAM.AUDIO_BITS - 1) - 1)
      x_wav = np.array(x_wav)
      y_wav = np.array(y_wav)
      y_wav_est = np.array(y_wav_est)
      x_wav = np.where(x_wav>abs_max,abs_max,x_wav)
      x_wav = np.where(x_wav<-abs_max,-abs_max,x_wav)
      y_wav = np.where(y_wav>abs_max,abs_max,y_wav)
      y_wav = np.where(y_wav<-abs_max,-abs_max,y_wav)
      y_wav_est = np.where(y_wav_est>abs_max,abs_max,y_wav_est)
      y_wav_est = np.where(y_wav_est<-abs_max,-abs_max,y_wav_est)

      print('      |-Decode cost time:',(time.time()-time_save))
      time_save = time.time()
      print('  |-Calculating PESQ...')
      sys.stdout.flush()
      pesq_mat_t = audio_tool.get_batch_pesq_improvement(x_wav, y_wav, y_wav_est, i, set_name)
      pesq_ans_t = np.mean(pesq_mat_t,axis=-1)
      print('      |-Batch average mix-ref     PESQ :',pesq_ans_t[0])
      print('      |-Batch average enhance-ref PESQ :',pesq_ans_t[1])
      print('      |-Batch average improved    PESQ :',pesq_ans_t[2])
      print('      |-Calculate PESQ cost time:',(time.time()-time_save))

      time_save = time.time()
      print('  |-Calculating STOI...')
      sys.stdout.flush()
      stoi_mat_t = audio_tool.get_batch_stoi_improvement(x_wav, y_wav, y_wav_est)
      stoi_ans_t = np.mean(stoi_mat_t,axis=-1)
      print('      |-Batch average mix-ref     STOI :',stoi_ans_t[0])
      print('      |-Batch average enhance-ref STOI :',stoi_ans_t[1])
      print('      |-Batch average improved    STOI :',stoi_ans_t[2])
      print('      |-Calculate STOI cost time:',(time.time()-time_save))

      time_save = time.time()
      print('  |-Calculating SDR...')
      sys.stdout.flush()
      sdr_mat_t = audio_tool.get_batch_sdr_improvement(x_wav, y_wav, y_wav_est)
      sdr_ans_t = np.mean(sdr_mat_t,axis=-1)
      # print(np.shape(sdr_mat_t),np.shape(sdr_ans_t))
      print('      |-Batch average mix-ref     SDR :',sdr_ans_t[0])
      print('      |-Batch average enhance-ref SDR :',sdr_ans_t[1])
      print('      |-Batch average improved    SDR :',sdr_ans_t[2])
      print('      |-Calculate SDR cost time:',(time.time()-time_save))
      sys.stdout.flush()

      if pesq_mat is None:
        pesq_mat = pesq_mat_t
        stoi_mat = stoi_mat_t
        sdr_mat = sdr_mat_t
      else:
        pesq_mat = np.concatenate((pesq_mat,pesq_mat_t),axis=-1)
        stoi_mat = np.concatenate((stoi_mat,stoi_mat_t),axis=-1)
        sdr_mat = np.concatenate((sdr_mat,sdr_mat_t),axis=-1)
    except tf.errors.OutOfRangeError:
      break
  pesq_ans = np.mean(pesq_mat,axis=-1)
  stoi_ans = np.mean(stoi_mat,axis=-1)
  sdr_ans = np.mean(sdr_mat,axis=-1)
  print('avg_pesq      raw:',pesq_ans[0])
  print('avg_pesq enhanced:',pesq_ans[1])
  print('avg_pesq      imp:',pesq_ans[2])
  print('avg_stoi      raw:',stoi_ans[0])
  print('avg_stoi enhanced:',stoi_ans[1])
  print('avg_stoi      imp:',stoi_ans[2])
  print('avg_sdr      raw:',sdr_ans[0])
  print('avg_sdr enhanced:',sdr_ans[1])
  print('avg_sdr      imp:',sdr_ans[2])
  return {'pesq':list(pesq_ans), 'stoi':list(stoi_ans), 'sdr':list(sdr_ans)}

def test_CC_or_OC(test_set_name):
  ckpt_dir = PARAM.CHECK_POINT
  _, _, testcc_tfrecords_dir, testoc_tfrecords_dir = generate_tfrecord(
      gen=PARAM.GENERATE_TFRECORD)
  if test_set_name == 'test_cc':
    tfrecord_dir = testcc_tfrecords_dir
  elif test_set_name == 'test_oc':
    tfrecord_dir = testoc_tfrecords_dir
  else:
    print(test_set_name,'not exist.')
    exit(-1)

  pesq_ans, stoi_ans, sdr_ans = get_PESQ_STOI_SDR(tfrecord_dir, ckpt_dir, set_name=test_set_name)
  # print(pesq_ans)
  # print(stoi_ans)
  # print(sdr_ans)
  # with open('test_ans.log','a+') as f:
  #   f.write(pesq_ans)
  #   f.write('\n')
  #   f.write(stoi_ans)
  #   f.write('\n')
  #   f.write(sdr_ans)
  #   f.write('\n')

if __name__ == "__main__":
  os.environ['CUDA_VISIBLE_DEVICES'] = sys.argv[1]
  # os.environ['OMP_NUM_THREADS'] = '8'
  tf.logging.set_verbosity(tf.logging.INFO)
  print('FLAGS.PARAM:')
  supper_dict = FLAGS.base_config.__dict__
  self_dict = PARAM.__dict__
  self_dict_keys = self_dict.keys()
  for key,val in supper_dict.items():
    if key in self_dict_keys:
      print('%s:%s' % (key,self_dict[key]))
    else:
      print('%s:%s' % (key,val))
  print('\n'.join(['%s:%s' % item for item in PARAM.__dict__.items()]))
  test_CC_or_OC(str(sys.argv[2]))
  # OMP_NUM_THREADS=1 python3 2_test_CC_and_OC.py "" test_cc 2>&1 | tee  exp/rnn_speech_enhancement/nnet_CX_mixedPhase_testcc.log
  # OMP_NUM_THREADS=1 python3 2_test_CC_and_OC.py "" test_oc 2>&1 | tee  exp/rnn_speech_enhancement/nnet_CX_mixedPhase_testoc.log
