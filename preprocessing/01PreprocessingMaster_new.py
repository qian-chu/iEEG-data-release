import json
import numpy as np
import mne
import os

from pathlib import Path
from mne_bids import BIDSPath, read_raw_bids
from HelperFunctions import (notch_filtering, \
                             mne_data_saver, plot_channels_psd, description_ch_rejection,
                             plot_bad_channels, custom_car, laplacian_referencing,
                             create_dig_montage, compute_hg, compute_erp, epoching, roi_mapping,
                             plot_electrode_localization)

SUPPORTED_STEPS = [
    "notch_filtering",
    "automated_bad_channels_rejection",
    "manual_artifact_detection",
    "car",
    "laplace_reference",
    "hg_computations",
    "manual_artifact_detection",
    "epoching",
    "automated_artifact_detection",
    "plot_epochs"
]

ERROR_UNKNOWN_STEP_TEXT = "You have given the preprocessing step: {step} in the analysis paramters json file that is " \
                          "not \nsupported. The supported steps are those: " \
                          "{supported_steps}. " \
                          "\nMake sure you check the spelling in the analysis parameter json file!"

ERROR_RAW_MISSING = "You have called {step} after calling epoching. This step only works if " \
                    "\n the signal is continuous. Make sure you set the order of your " \
                    "\npreprocessing steps such that this step is called BEFORE you call epoching"
ERROR_EPOCHS_MISSING = "You have called {step} before calling epoching. This step only works if " \
                       "\n the signal is epoched already. Make sure you set the order of your " \
                       "\npreprocessing steps such that this step is called AFTER you call epoching"
ERROR_SIGNAL_MISSING = "For the preprocessing step {step}, you have passed the signal {signal} " \
                       "\nwhich does not exist at this stage of the preprocessing. Either you have asked " \
                       "\nfor that signal to be generated later OR you haven't asked for it to be " \
                       "\ngenerated. Make sure to check your config file"


def preprocessing(config, subjects):
    print("-" * 40)
    print("Welcome to preprocessing!")
    print("The following subjects will now be preprocessed: ")
    print(subjects)
    print("Using the config file:")
    print(config)
    print("It may take some time, count roughly 5-10min per subject!")
    if isinstance(subjects, str):
        subjects = [subjects]

    # ======================================================================================================
    # Looping through each subject:
    for subject in subjects:
        print("-" * 40)
        print("Preprocessing {} with config file {}".format(subject, config))

        # ======================================================================================================
        # Load the config:
        with open(config) as f:
            param = json.load(f)

        # Create path to save the data:
        save_root = Path(param["bids_root"], 'derivatives', 'preprocessing',
                         'sub-' + subject, 'ses-' + param["session"], param["data_type"])
        if not os.path.isdir(save_root):
            # Creating the directory:
            os.makedirs(save_root)

        # Create the file prefix:
        file_prefix = 'sub-{}_ses-{}_task-{}'.format(subject, param["session"], param["task"])

        # --------------------------------------------------------------------------------------------------------------
        # Preparing the data:
        # --------------------------------------------------------------------------------------------------------------
        # Creating the bids path object:
        bids_path = BIDSPath(root=param["bids_root"], subject=subject,
                             session=param["session"],
                             datatype=param["data_type"],
                             task=param["task"])
        # Loading the data under the term broadband, as it is what they are as long as no further
        # filtering was employed
        raw = {"broadband": read_raw_bids(bids_path=bids_path, verbose=True)}

        # Create the montage, as mne bids doesn't handle having electrodes localization in two different
        # spaces in the bids directory as in our case
        raw["broadband"] = create_dig_montage(raw["broadband"], bids_path, montage_space=param["montage_space"])

        # Load the data in memory:
        raw["broadband"].load_data()
        # Downsampling the signal, otherwise things take forever:
        print("Downsampling the signal to {}Hz, this may take a little while".format(param['downsample_rate']))
        raw["broadband"].resample(param['downsample_rate'], n_jobs=param["njobs"], verbose=True)
        print(raw["broadband"].info)

        # Detrending the data:
        print("Detrending the data")
        raw["broadband"].apply_function(lambda ch: ch - np.mean(ch),
                                        n_jobs=param["njobs"],
                                        channel_wise=True)

        # Create the events in the signal from the annotation for later use:
        print('Creating annotations')
        events_from_annot, event_dict = mne.events_from_annotations(
            raw["broadband"])
        # And then deleting the annotations, not needed anymore. Makes interactive plotting stuff more reactive:
        raw["broadband"].set_annotations(mne.Annotations(onset=[], duration=[], description=[]))

        # ==============================================================================================================
        # Preprocessing loop
        # ==============================================================================================================
        for step in param["preprocessing_steps"]:

            # ----------------------------------------------------------------------------------------------------------
            # Notch filter:
            # ----------------------------------------------------------------------------------------------------------
            if step.lower() == "notch_filtering":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                # Looping through the different signals that are requested to be filtered:
                for ind, signal in enumerate(list(step_parameters.keys())):
                    if 'raw' in locals() and signal in raw:
                        # Filtering the signal:
                        raw[signal] = notch_filtering(raw[signal],
                                                      njobs=param["njobs"],
                                                      **step_parameters[signal])
                        # Saving the data:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step, signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after notch filtering")
                            plot_channels_psd(raw[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["channel_types"])
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # Description based bad channels rejection (based on the BIDS channels.tsv
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "description_bad_channels_rejection":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                for ind, signal in enumerate(list(step_parameters.keys())):
                    if 'raw' in locals() and signal in raw:
                        raw[signal], bad_channels = \
                            description_ch_rejection(
                                raw[signal], bids_path,
                                step_parameters[signal]["bad_channels_description"],
                                discard_bads=step_parameters[signal]["discard_bads"])
                        # Saving the data:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step,  signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the bad channels after notch description_bad_channels_rejection")
                            plot_bad_channels(raw[signal], param, save_root, step, signal, file_prefix,
                                              file_extension="-bads.png", plot_single_channels=False,
                                              picks="bads")
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # Common average referencing:
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "car":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                # Looping through the signals we want to perform the CAR on:
                for ind, signal in enumerate(list(step_parameters.keys())):
                    if 'raw' in locals() and signal in raw:
                        raw[signal] = custom_car(
                            raw[signal], **step_parameters[signal])
                        # Saving the data:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step,  signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after common average referencing")
                            plot_channels_psd(raw[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["channel_types"])
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # Laplacian referencing:
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "laplace_reference":
                print("-" * 60)
                print("Performing " + step)
                # Looping through the signals we want to perform the laplace reference on:
                step_parameters = param[step]
                for ind, signal in enumerate(list(step_parameters.keys())):
                    if 'raw' in locals() and signal in raw:
                        # Ploting the channels PSD before the laplace referencing to see changes:
                        # Generate the laplace mapping file name:
                        laplace_mapping_file = "sub-{0}_ses-" + param["session"] + \
                                               "_laplace_mapping_" + param["data_type"] + ".json"
                        sub_laplace_mapping = Path(bids_path.directory,
                                                   laplace_mapping_file.format(subject))
                        if sub_laplace_mapping.is_file():
                            with open(sub_laplace_mapping) as f:
                                laplacian_mapping = json.load(f)
                        else:  # If this is missing, the laplace mapping cannot be performed!
                            raise FileNotFoundError("The laplace mapping json is missing: {0}"
                                                    "\nYou need to generate a laplace mapping json file before you can"
                                                    "preprocessing this"
                                                    "\nspecific step."
                                                    "\nCheck out the script semi_automated_laplace_mapping.py".
                                                    format(sub_laplace_mapping))
                        # Performing the laplace referencing:
                        raw[signal], reference_mapping, bad_channels = \
                            laplacian_referencing(raw[signal], laplacian_mapping,
                                                  subjects_dir=param["fs_dir"],
                                                  subject="sub-" + subject,
                                                  montage_space=param["montage_space"],
                                                  **step_parameters[signal])
                        # Saving the data:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step,  signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after laplace referencing")
                            plot_channels_psd(raw[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["channel_types"])
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # high_gamma_computations
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "hg_computations":
                print("-" * 60)
                print("Performing " + step)
                # Looping through the signals we want to perform the laplace reference on:
                step_parameters = param[step]
                if 'raw' in locals():
                    # Looping through the different signals bands to compute:
                    for signal in step_parameters.keys():
                        # Extract the parameters for this specific frequency band:
                        frequency_band_parameters = step_parameters[signal]
                        # Compute the signal accordingly:
                        raw[signal] = compute_hg(
                            raw[frequency_band_parameters["source_signal"]],
                            njobs=param["njobs"],
                            **frequency_band_parameters["computation_parameters"])
                        # Plotting the psd:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step, signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after high gamma computations")
                            plot_channels_psd(raw[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["computation_parameters"][
                                                  "channel_types"])
                elif 'raw' not in locals():
                    raise Exception(ERROR_RAW_MISSING.format(step=step))
                elif signal not in raw:
                    raise Exception(ERROR_SIGNAL_MISSING.format(
                        step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # erp_computations
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "erp_computations":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                if 'raw' in locals():
                    # Looping through the different signals bands to compute:
                    for signal in step_parameters.keys():
                        # Compute the high gamma:
                        raw[signal] = \
                            compute_erp(raw[step_parameters[signal]["source_signal"]],
                                        njobs=param["njobs"],
                                        **step_parameters[signal]["computation_parameters"])

                        # Plotting the psd:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(raw[signal], param, save_root, step, signal, file_prefix,
                                           file_extension="-raw.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after filtering")
                            plot_channels_psd(raw[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["channel_types"])
                elif 'raw' not in locals():
                    raise Exception(ERROR_RAW_MISSING.format(step=step))
                elif signal not in raw:
                    raise Exception(ERROR_SIGNAL_MISSING.format(
                        step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # epoching
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "epoching":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                # The epochs will be stored in a dictionary. Needs to be created first:
                epochs = {}
                for signal in list(step_parameters.keys()):
                    print(signal)
                    if 'raw' in locals() and signal in raw:
                        epochs[signal] = epoching(raw[signal], events_from_annot, event_dict,
                                                  **step_parameters[signal])
                        # Saving the data:
                        if param["save_intermediary_steps"]:
                            mne_data_saver(epochs[signal], param, save_root, step, signal, file_prefix,
                                           file_extension="-epo.fif")
                        if param["check_plots"]:
                            print("-" * 40)
                            print("Plotting the channels power spectral density after epoching")
                            plot_channels_psd(epochs[signal], param,
                                              save_root, step,
                                              signal, file_prefix,
                                              file_extension="-psd.png", plot_single_channels=False,
                                              channels_type=step_parameters[signal]["channel_types"])
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

                # Deleting the raw after we did the epoching. This is to avoid the case where the user has set a step
                # that is to be performed on the non segmented data after the epoching was done.
                del raw

            # ----------------------------------------------------------------------------------------------------------
            # atlas_mapping (i.e. determine channels anatomical labels)
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "atlas_mapping":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                # First, relocating the free surfer directory:
                subject_free_surfer_dir = Path(
                    param["fs_dir"], "sub-" + subject)
                assert subject_free_surfer_dir.is_dir(), ("The free surfer reconstruction is not available for this "
                                                          "subject! Make sure to download it")

                # Getting the anatomical labels
                for signal in step_parameters.keys():  # The localization should be the same across signal, but kept for
                    # compatibility
                    if "raw" in locals():
                        # Extract the anatomical labels:
                        electrodes_mapping_df = roi_mapping(raw["broadband"], step_parameters["list_parcellations"],
                                                            "sub-" + subject, param["fs_dir"], config, save_root, step,
                                                            signal, file_prefix, file_extension='mapping.csv')

                        # Get a list of the channels that are outside the brain:
                        roi_df = electrodes_mapping_df[list(electrodes_mapping_df.keys())[0]]
                        bad_channels = []
                        # Looping through each channel to see if the only label is "unknown":
                        for channel in list(roi_df["channel"]):
                            # Get the rois of this channel:
                            ch_roi = roi_df.loc[roi_df["channel"] == channel, "region"].item()
                            # Check whether this channel is labelled only as "unknow":
                            if len(ch_roi.split("/")) == 1:
                                if ch_roi.split("/")[0].lower() == "unknown":
                                    bad_channels.append(channel)
                        if step_parameters["remove_channels_unknown"]:
                            print("WARNING: The following channels were found to sit outside the brain and will be set "
                                  "to bad!")
                            print(bad_channels)
                            # Set these channels as bad:
                            raw["broadband"].info['bads'].extend(bad_channels)

                    elif "epochs" in locals():
                        # Doing the probabilistic mapping onto the different atlases:
                        # Extract the anatomical labels:
                        electrodes_mapping_df = roi_mapping(epochs["broadband"], step_parameters["list_parcellations"],
                                                            "sub-" + subject, param["fs_dir"], config, save_root, step,
                                                            signal, file_prefix, file_extension='mapping.csv')

                        # Get a list of the channels that are outside the brain:
                        roi_df = electrodes_mapping_df[list(electrodes_mapping_df.keys())[0]]
                        bad_channels = []
                        # Looping through each channel to see if the only label is "unknown":
                        for channel in list(roi_df["channel"]):
                            # Get the rois of this channel:
                            ch_roi = roi_df.loc[roi_df["channel"] == channel, "region"].item()
                            # Check whether this channel is labelled only as "unknow":
                            if len(ch_roi.split("/")) == 1:
                                if ch_roi.split("/")[0].lower() == "unknown":
                                    bad_channels.append(channel)
                        if step_parameters["remove_channels_unknown"]:
                            print("WARNING: The following channels were found to sit outside the brain and will be set "
                                  "to bad!")
                            print(bad_channels)
                            # Set these channels as bad:
                            epochs["broadband"].info['bads'].extend(bad_channels)
                    elif 'raw' not in locals():
                        raise Exception(ERROR_RAW_MISSING.format(step=step))
                    elif signal not in raw:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))
                    elif signal not in epochs:
                        raise Exception(ERROR_SIGNAL_MISSING.format(
                            step=step, signal=signal))

            # ----------------------------------------------------------------------------------------------------------
            # Plot channels localization on brain 3D surface:
            # ----------------------------------------------------------------------------------------------------------
            elif step.lower() == "plot_channels_loc":
                print("-" * 60)
                print("Performing " + step)
                # Get the parameters of this specific step:
                step_parameters = param[step]
                # First, relocating the free surfer directory:
                subject_free_surfer_dir = Path(
                    param["fs_dir"], "sub-" + subject)
                assert subject_free_surfer_dir.is_dir(), ("The free surfer reconstruction is not available for this "
                                                          "subject! Make sure to download it")

                # Plotting the electrodes localization:
                for signal in step_parameters.keys():  # The localization should be the same across signal, but kept for
                    # compatibility
                    if "raw" in locals():
                        # Plotting the electrodes localization on the brain surface
                        plot_electrode_localization(raw["broadband"].copy(), 'sub-' + subject, param["fs_dir"], param,
                                                    save_root, step, signal, file_prefix,
                                                    montage_space=param["montage_space"], file_extension='-loc.png',
                                                    channels_to_plot=step_parameters[signal]["channel_types"],
                                                    plot_elec_name=False)
                        plot_electrode_localization(raw["broadband"].copy(), 'sub-' + subject, param["fs_dir"], param,
                                                    save_root, step, signal, file_prefix,
                                                    montage_space=param["montage_space"], file_extension='-loc.png',
                                                    channels_to_plot=step_parameters[signal]["channel_types"],
                                                    plot_elec_name=True)

                    elif "epochs" in locals():
                        # Plotting the electrodes localization on the brain surface
                        plot_electrode_localization(epochs["broadband"].copy(), 'sub-' + subject, param["fs_dir"],
                                                    param, save_root, step, signal, file_prefix,
                                                    montage_space=param["montage_space"], file_extension='-loc.png',
                                                    channels_to_plot=step_parameters[signal]["channel_types"],
                                                    plot_elec_name=False)
                        plot_electrode_localization(epochs["broadband"].copy(), 'sub-' + subject, param["fs_dir"],
                                                    param, save_root, step, signal, file_prefix,
                                                    montage_space=param["montage_space"], file_extension='-loc.png',
                                                    channels_to_plot=step_parameters[signal]["channel_types"],
                                                    plot_elec_name=True)

            # ----------------------------------------------------------------------------------------------------------
            # Raise in error if the step that was passed isn't supported. This is to avoid the user assuming that
            # something was done when it wasn't.
            else:
                raise Exception(ERROR_UNKNOWN_STEP_TEXT.format(
                    step=step, supported_steps=SUPPORTED_STEPS))


if __name__ == "__main__":
    config_file = r"C:\Users\alexander.lepauvre\Documents\GitHub\iEEG-data-release\preprocessing\PreprocessingParameters_task-Duration_release.json"
    subjects_list = ["SF102"]
    preprocessing(config_file, subjects_list)

