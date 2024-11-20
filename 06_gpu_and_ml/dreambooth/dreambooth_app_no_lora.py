# ---
# deploy: true
# tags: ["use-case-image-video-3d", "use-case-finetuning", "featured"]
# ---
#
# # Custom Pet Art from Flux with Hugging Face and Gradio
#
# This example finetunes the [Flux.1-dev model](https://huggingface.co/black-forest-labs/FLUX.1-dev)
# on images of a pet (by default, a puppy named Qwerty)
# using a technique called textual inversion from [the "Dreambooth" paper](https://dreambooth.github.io/).
# Effectively, it teaches a general image generation model a new "proper noun",
# allowing for the personalized generation of art and photos.
#
# It then makes the model shareable with others -- without costing $25/day for a GPU server--
# by hosting a [Gradio app](https://gradio.app/) on Modal.
#
# It demonstrates a simple, productive, and cost-effective pathway
# to building on large pretrained models using Modal's building blocks, like
# [GPU-accelerated](https://modal.com/docs/guide/gpu) Modal functions and classes for compute-intensive work,
# [volumes](https://modal.com/docs/guide/volumes) for storage,
# and [web endpoints](https://modal.com/docs/guide/webhooks) for serving.
#
# And with some light customization, you can use it to generate images of your pet!
#
# ![Gradio.app image generation interface](./gradio-image-generate.png)
#
# You can find a video walkthrough of this example on the Modal YouTube channel
# [here](https://www.youtube.com/watch?v=df-8fiByXMI).
#
# ## Imports and setup
#
# We start by importing the necessary libraries and setting up the environment.
# By installing Modal, we already brought in the FastAPI library we'll use to serve our app,
# so we import it here.

import itertools
from dataclasses import dataclass
from pathlib import Path

import modal
from fastapi import FastAPI
from fastapi.responses import FileResponse

# ## Building up the environment
#
# Machine learning environments are complex, and the dependencies can be hard to manage.
# Modal makes creating and working with environments easy via containers and container images.
#
# We start from a base image and specify all of our dependencies.
# We'll call out the interesting ones as they come up below.
# Note that these dependencies are not installed locally
# -- they are only installed in the remote environment where our app runs.

app = modal.App(name="example-dreambooth-flux")

image = modal.Image.debian_slim(python_version="3.10").pip_install(
    "accelerate==0.31.0",
    "datasets==3.1.0",
    "ftfy~=6.1.0",
    "gradio~=4.44.1",
    "fastapi[standard]==0.115.4",
    "numpy==1.26.4",
    "pydantic==2.9.2",
    "starlette==0.41.2",
    "smart_open~=6.4.0",
    "transformers~=4.41.2",
    "sentencepiece>=0.1.91,!=0.1.92",
    "torch~=2.2.0",
    "torchvision~=0.16",
    "triton~=2.2.0",
    "peft==0.11.1",
    "wandb==0.17.6",
)

# ### Downloading scripts and installing a git repo with `run_commands`
#
# We'll use an example script from the `diffusers` library to train the model.
# We acquire it from GitHub and install it in our environment with a series of commands.
# The container environments Modal functions run in are highly flexible --
# see [the docs](https://modal.com/docs/guide/custom-container) for more details.

GIT_SHA = (
    "2541d141d5ffa9c94a7e8f5ca7f4ada26bad811d"  # specify the commit to fetch
)

image = (
    image.apt_install("git")
    # Perform a shallow fetch of just the target `diffusers` commit, checking out
    # the commit in the container's home directory, /root. Then install `diffusers`
    .run_commands(
        "cd /root && git init .",
        "cd /root && git remote add origin https://github.com/huggingface/diffusers",
        f"cd /root && git fetch --depth=1 origin {GIT_SHA} && git checkout {GIT_SHA}",
        "cd /root && pip install -e .",
    )
)
# ### Configuration with `dataclass`es
#
# Machine learning apps often have a lot of configuration information.
# We collect up all of our configuration into dataclasses to avoid scattering special/magic values throughout code.


@dataclass
class SharedConfig:
    """Configuration information shared across project components."""

    # The instance name is the "proper noun" we're teaching the model
    instance_name: str = "Heroicon"
    # That proper noun is usually a member of some class (person, bird),
    # and sharing that information with the model helps it generalize better.
    class_name: str = "style"
    # identifier for pretrained models on Hugging Face
    model_name: str = "black-forest-labs/FLUX.1-dev"


# ### Downloading weights with `run_function`
#
# Not everything we need for an ML app like Pet Dreambooth is available as a Python package
# or even on GitHub. Sometimes, there is nothing to be done but to execute some code inside the environment.
# We can do this on Modal with `run_function`.
#
# In our case, we use it to download the pretrained model weights for the Stable Diffusion XL model
# that we'll be finetuning.
#
# Note that access to the Flux.1-dev model on Hugging Face is
# [gated by a license agreement](https://huggingface.co/docs/hub/en/models-gated) which
# you must agree to [here](https://huggingface.co/black-forest-labs/FLUX.1-dev).
# After you have accepted the license, [create a Modal Secret](https://modal.com/secrets)
# with the name `huggingface` following the instructions in the template.


def download_models():
    from diffusers import DiffusionPipeline
    from transformers.utils import move_cache

    config = SharedConfig()

    DiffusionPipeline.from_pretrained(config.model_name, force_download=True)
    move_cache()


image = image.run_function(
    download_models, secrets=[modal.Secret.from_name("huggingface")]
)


# ### Storing data generated by our app with `modal.Volume`
#
# The tools we've used so far work well for fetching external information,
# which defines the environment our app runs in,
# but what about data that we create or modify during the app's execution?
# A persisted `modal.Volume` can store and share data across Modal apps or runs of the same app.
#
# We'll use one to store the fine-tuned weights we create during training
# and then load them back in for inference.

volume = modal.Volume.from_name(
    "dreambooth-volume-flux-heroicons-11-16-no-lora",
    create_if_missing=True,
)
MODEL_DIR = "/model"


# ### Load finetuning dataset
#
# Part of the magic of the Dreambooth approach is that we only need 3-10 images for finetuning.
# So we can fetch just a few images, stored on consumer platforms like Imgur or Google Drive,
# whenever we need them -- no need for expensive, hard-to-maintain data pipelines.


def load_images(image_urls: list[str]) -> Path:
    import PIL.Image
    from smart_open import open

    img_path = Path("/img")

    img_path.mkdir(parents=True, exist_ok=True)
    for ii, url in enumerate(image_urls):
        with open(url, "rb") as f:
            image = PIL.Image.open(f)
            image.save(img_path / f"{ii}.png")
    print(f"{ii + 1} images loaded")

    return img_path


# ## Finetuning a text-to-image model
#
# The base model we start from is trained to do a sort of "reverse [ekphrasis](https://en.wikipedia.org/wiki/Ekphrasis)":
# it attempts to recreate a visual work of art or image from only its description.
#
# We can use the model to synthesize wholly new images
# by combining the concepts it has learned from the training data.
#
# We use a pretrained model, the XL version of Stability AI's Stable Diffusion.
# In this example, we "finetune" SDXL, making only small adjustments to the weights.
# Furthermore, we don't change all the weights in the model.
# Instead, using a technique called [_low-rank adaptation_](https://arxiv.org/abs/2106.09685),
# we change a much smaller matrix that works "alongside" the existing weights, nudging the model in the direction we want.
#
# We can get away with such a small and simple training process because we're just teach the model the meaning of a single new word: the name of our pet.
#
# The result is a model that can generate novel images of our pet:
# as an astronaut in space, as painted by Van Gogh or Bastiat, etc.
#
# ### Finetuning with Hugging Face 🧨 Diffusers and Accelerate
#
# The model weights, training libraries, and training script are all provided by [🤗 Hugging Face](https://huggingface.co).
#
# You can kick off a training job with the command `modal run dreambooth_app.py::app.train`.
# It should take about ten minutes.
#
# Training machine learning models takes time and produces a lot of metadata --
# metrics for performance and resource utilization,
# metrics for model quality and training stability,
# and model inputs and outputs like images and text.
# This is especially important if you're fiddling around with the configuration parameters.
#
# This example can optionally use [Weights & Biases](https://wandb.ai) to track all of this training information.
# Just sign up for an account, switch the flag below, and add your API key as a [Modal secret](https://modal.com/docs/guide/secrets).

USE_WANDB = True

# You can see an example W&B dashboard [here](https://wandb.ai/cfrye59/dreambooth-lora-sd-xl).
# Check out [this run](https://wandb.ai/cfrye59/dreambooth-lora-sd-xl/runs/ca3v1lsh?workspace=user-cfrye59),
# which [despite having high GPU utilization](https://wandb.ai/cfrye59/dreambooth-lora-sd-xl/runs/ca3v1lsh/system)
# suffered from numerical instability during training and produced only black images -- hard to debug without experiment management logs!
#
# You can read more about how the values in `TrainConfig` are chosen and adjusted [in this blog post on Hugging Face](https://huggingface.co/blog/dreambooth).
# To run training on images of your own pet, upload the images to separate URLs and edit the contents of the file at `TrainConfig.instance_example_urls_file` to point to them.
#
# Tip: if the results you're seeing don't match the prompt too well, and instead produce an image
# of your subject without taking the prompt into account, the model has likely overfit. In this case, repeat training with a lower
# value of `max_train_steps`. If you used W&B, look back at results earlier in training to determine where to stop.
# On the other hand, if the results don't look like your subject, you might need to increase `max_train_steps`.


@dataclass
class TrainConfig(SharedConfig):
    """Configuration for the finetuning step."""

    # HuggingFace Hub dataset
    # dataset_name = "linoyts/3d_icon"
    dataset_name = "yirenlu/heroicons-subset-25-images"
    # caption_column = "prompt"
    caption_column = "text"

    instance_prompt = "an HCON, in the style of TOK"
    # instance_prompt = "3dicon, in the style of TOK"

    # training prompt looks like `{PREFIX} {INSTANCE_NAME} the {CLASS_NAME} {POSTFIX}`
    prefix: str = ""
    postfix: str = ""

    # Hyperparameters/constants from the huggingface training example
    # resolution: int = 1024
    resolution: int = 512
    train_batch_size: int = 1
    rank: int = 16  # lora rank
    gradient_accumulation_steps: int = 1
    learning_rate: float = 4e-4
    lr_scheduler: str = "constant"
    lr_warmup_steps: int = 0
    max_train_steps: int = 2000
    checkpointing_steps: int = 1000
    seed: int = 0


@dataclass
class SweepConfig(TrainConfig):
    """Configuration for hyperparameter sweep"""

    # Sweep parameters
    train_steps = [2000, 3000, 4000]
    ranks = [8]

    # Test prompts for evaluation
    threedicon_test_prompts = [
        "3d icon of the nike logo in the style of TOK",
        "3d icon of a mcdonalds sign in the style of TOK",
        "3d icon of a Google sign in the style of TOK",
        "3d icon of the mercedes benz sign in the style of TOK",
    ]

    heroicon_test_prompts = [
        # "An HCON, a black and white minimalist icon of a sailboat",
        # "An HCON, a black and white minimalist icon of a watch",
        # "An HCON, a black and white minimalist icon of a bear",
        # "An HCON, a black and white minimalist icon of the mcdonalds sign",
        # "An HCON, a black and white minimalist icon of a book",
        # "An HCON, a black and white minimalist icon of a cellphone",
        # "An HCON, a black and white minimalist icon of a water bottle",
        "An HCON, a black and white minimalist icon that represents the international monetary system",
        "An HCON, a black and white minimalist icon of a macbook pro laptop",
        "An HCON, a black and white minimalist icon of a tiara",
        "An HCON, a black and white minimalist icon of mountain peaks",
        "An HCON, a black and white minimalist icon of that represents a bank account",
    ]


def generate_sweep_configs(sweep_config: SweepConfig):
    """Generate all combinations of hyperparameters"""
    param_combinations = itertools.product(
        sweep_config.train_steps,
        sweep_config.ranks,
    )

    return [
        {
            "max_train_steps": steps,
            "rank": rank,
            "model_name": sweep_config.model_name,
            "instance_prompt": sweep_config.instance_prompt,
            "dataset_name": sweep_config.dataset_name,
            "caption_column": sweep_config.caption_column,
            "resolution": sweep_config.resolution,
            "train_batch_size": sweep_config.train_batch_size,
            "gradient_accumulation_steps": sweep_config.gradient_accumulation_steps,
            "lr_scheduler": sweep_config.lr_scheduler,
            "lr_warmup_steps": sweep_config.lr_warmup_steps,
            "checkpointing_steps": sweep_config.checkpointing_steps,
            "seed": sweep_config.seed,
            "output_dir": Path(MODEL_DIR) / f"steps_{steps}_rank_{rank}",
        }
        for steps, rank in param_combinations
    ]


@app.function(
    image=image,
    gpu=modal.gpu.A100(  # fine-tuning is VRAM-heavy and requires an A100 GPU
        count=2, size="80GB"
    ),
    volumes={MODEL_DIR: volume},  # stores fine-tuned model
    timeout=7200,  # 30 minutes
    secrets=[
        modal.Secret.from_name("wandb"),
        modal.Secret.from_name("huggingface"),
    ]
    if USE_WANDB
    else [modal.Secret.from_name("huggingface")],
)
def train(config):
    import subprocess

    from accelerate.utils import write_basic_config

    # load data locally

    # set up hugging face accelerate library for fast training
    write_basic_config(mixed_precision="bf16")

    # the model training is packaged as a script, so we have to execute it as a subprocess, which adds some boilerplate
    def _exec_subprocess(cmd: list[str]):
        """Executes subprocess and prints log to terminal while subprocess is running."""
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with process.stdout as pipe:
            for line in iter(pipe.readline, b""):
                line_str = line.decode()
                print(f"{line_str}", end="")

        if exitcode := process.wait() != 0:
            raise subprocess.CalledProcessError(exitcode, "\n".join(cmd))

    # run training -- see huggingface accelerate docs for details
    print("launching dreambooth training script")
    _exec_subprocess(
        [
            "accelerate",
            "launch",
            "examples/dreambooth/train_dreambooth_flux.py",
            "--mixed_precision=bf16",  # half-precision floats most of the time for faster training
            f"--pretrained_model_name_or_path={config['model_name']}",
            f"--dataset_name={config['dataset_name']}",
            f"--caption_column={config['caption_column']}",
            f"--output_dir={config['output_dir']}",
            f"--instance_prompt={config['instance_prompt']}",
            f"--resolution={config['resolution']}",
            f"--train_batch_size={config['train_batch_size']}",
            f"--gradient_accumulation_steps={config['gradient_accumulation_steps']}",
            "--optimizer='prodigy'",
            "--learning_rate=1.",
            f"--lr_scheduler={config['lr_scheduler']}",
            f"--lr_warmup_steps={config['lr_warmup_steps']}",
            f"--max_train_steps={config['max_train_steps']}",
            f"--checkpointing_steps={config['checkpointing_steps']}",
            f"--seed={config['seed']}",  # increased reproducibility by seeding the RNG
        ]
        + (
            [
                "--report_to=wandb",
                # validation output tracking is useful, but currently broken for Flux LoRA training
                # f"--validation_prompt={prompt} in space",  # simple test prompt
                # f"--validation_epochs={config['max_train_steps'] // 5}",
            ]
            if USE_WANDB
            else []
        ),
    )
    # The trained model information has been output to the volume mounted at `MODEL_DIR`.
    # To persist this data for use in our web app, we 'commit' the changes
    # to the volume.
    volume.commit()
    return config


# ## Running our model
#
# To generate images from prompts using our fine-tuned model, we define a Modal function called `inference`.
#
# Naively, this would seem to be a bad fit for the flexible, serverless infrastructure of Modal:
# wouldn't you need to include the steps to load the model and spin it up in every function call?
#
# In order to initialize the model just once on container startup,
# we use Modal's [container lifecycle](https://modal.com/docs/guide/lifecycle-functions) features, which require the function to be part
# of a class. Note that the `modal.Volume` we saved the model to is mounted here as well,
# so that the fine-tuned model created  by `train` is available to us.


def evaluate_model(hyperparameter_model_dir, wandb_run):
    """Load trained model and evaluate on test prompts"""
    import wandb

    # Generate images for test prompts
    sweep_config = SweepConfig()
    app_config = AppConfig()
    for image, prompt in Model(hyperparameter_model_dir).inference.starmap(
        [(x, app_config) for x in sweep_config.heroicon_test_prompts]
    ):
        wandb_run.log(
            {
                f"test_image/{hyperparameter_model_dir}/{prompt}": wandb.Image(
                    image
                )
            }
        )


@app.cls(image=image, gpu="A100", volumes={MODEL_DIR: volume})
class Model:
    def __init__(self, hyperparameter_model_dir):
        self.hyperparameter_model_dir = hyperparameter_model_dir

    @modal.enter()
    def load_model(self):
        import torch
        from diffusers import DiffusionPipeline

        # Reload the modal.Volume to ensure the latest state is accessible.
        volume.reload()

        # set up a hugging face inference pipeline using our model
        pipe = DiffusionPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-dev",
            torch_dtype=torch.bfloat16,
        ).to("cuda")
        pipe.load_lora_weights(f"{MODEL_DIR}/{self.hyperparameter_model_dir}")
        self.pipe = pipe
        # import torch
        # from diffusers import AutoPipelineForText2Image
        # from safetensors.torch import load_file

        # config = TrainConfig()

        # # Reload the modal.Volume to ensure the latest state is accessible.
        # volume.reload()

        # # set up a hugging face inference pipeline using our model
        # pipe = AutoPipelineForText2Image.from_pretrained(
        #     config.model_name,
        #     torch_dtype=torch.bfloat16,
        # ).to("cuda")
        # pipe.load_lora_weights(
        #     MODEL_DIR, weight_name="pytorch_lora_weights.safetensors"
        # )

        # embedding_path = "/model/model_emb.safetensors"

        # state_dict = load_file(embedding_path)
        # # load embeddings of text_encoder 1 (CLIP ViT-L/14)
        # pipe.load_textual_inversion(
        #     state_dict["clip_l"],
        #     token=["<s0>", "<s1>"],
        #     text_encoder=pipe.text_encoder,
        #     tokenizer=pipe.tokenizer,
        # )

        # self.pipe = pipe

    @modal.method()
    def inference(self, text, config):
        image = self.pipe(
            text,
            num_inference_steps=config.num_inference_steps,
        ).images[0]

        return (image, text)


# ## Wrap the trained model in a Gradio web UI
#
# [Gradio](https://gradio.app) makes it super easy to expose a model's functionality
# in an easy-to-use, responsive web interface.
#
# This model is a text-to-image generator,
# so we set up an interface that includes a user-entry text box
# and a frame for displaying images.
#
# We also provide some example text inputs to help
# guide users and to kick-start their creative juices.
#
# And we couldn't resist adding some Modal style to it as well!
#
# You can deploy the app on Modal with the command
# `modal deploy dreambooth_app.py`.
# You'll be able to come back days, weeks, or months later and find it still ready to go,
# even though you don't have to pay for a server to run while you're not using it.

web_app = FastAPI()
assets_path = Path(__file__).parent / "assets"


@dataclass
class AppConfig(SharedConfig):
    """Configuration information for inference."""

    num_inference_steps: int = 25
    guidance_scale: float = 6


@app.function(
    image=image,
    concurrency_limit=1,
    allow_concurrent_inputs=1000,
    mounts=[modal.Mount.from_local_dir(assets_path, remote_path="/assets")],
)
@modal.asgi_app()
def fastapi_app():
    import gradio as gr
    from gradio.routes import mount_gradio_app

    # Call out to the inference in a separate Modal environment with a GPU
    def go(text=""):
        if not text:
            text = example_prompts[0]
        print(text, config)
        return Model().inference.remote(text, config)

    # set up AppConfig
    config = AppConfig()

    instance_phrase = f"{config.instance_name} the {config.class_name}"

    example_prompts = [
        f"{instance_phrase}",
        f"a painting of {instance_phrase.title()} With A Pearl Earring, by Vermeer",
        f"oil painting of {instance_phrase} flying through space as an astronaut",
        f"a painting of {instance_phrase} in cyberpunk city. character design by cory loftis. volumetric light, detailed, rendered in octane",
        f"drawing of {instance_phrase} high quality, cartoon, path traced, by studio ghibli and don bluth",
    ]

    modal_docs_url = "https://modal.com/docs"
    modal_example_url = f"{modal_docs_url}/examples/dreambooth_app"

    description = f"""Describe what they are doing or how a particular artist or style would depict them. Be fantastical! Try the examples below for inspiration.

### Learn how to make a "Dreambooth" for your own pet [here]({modal_example_url}).
    """

    # custom styles: an icon, a background, and a theme
    @web_app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse("/assets/favicon.svg")

    @web_app.get("/assets/background.svg", include_in_schema=False)
    async def background():
        return FileResponse("/assets/background.svg")

    with open("/assets/index.css") as f:
        css = f.read()

    theme = gr.themes.Default(
        primary_hue="green", secondary_hue="emerald", neutral_hue="neutral"
    )

    # add a gradio UI around inference
    with gr.Blocks(
        theme=theme, css=css, title="Pet Dreambooth on Modal"
    ) as interface:
        gr.Markdown(
            f"# Dream up images of {instance_phrase}.\n\n{description}",
        )
        with gr.Row():
            inp = gr.Textbox(  # input text component
                label="",
                placeholder=f"Describe the version of {instance_phrase} you'd like to see",
                lines=10,
            )
            out = gr.Image(  # output image component
                height=512, width=512, label="", min_width=512, elem_id="output"
            )
        with gr.Row():
            btn = gr.Button("Dream", variant="primary", scale=2)
            btn.click(
                fn=go, inputs=inp, outputs=out
            )  # connect inputs and outputs with inference function

            gr.Button(  # shameless plug
                "⚡️ Powered by Modal",
                variant="secondary",
                link="https://modal.com",
            )

        with gr.Column(variant="compact"):
            # add in a few examples to inspire users
            for ii, prompt in enumerate(example_prompts):
                btn = gr.Button(prompt, variant="secondary")
                btn.click(fn=lambda idx=ii: example_prompts[idx], outputs=inp)

    # mount for execution on Modal
    return mount_gradio_app(
        app=web_app,
        blocks=interface,
        path="/",
    )


# ## Running your own Dreambooth from the command line
#
# You can use the `modal` command-line interface to set up, customize, and deploy this app:
#
# - `modal run dreambooth_app.py` will train the model. Change the `instance_example_urls_file` to point to your own pet's images.
# - `modal serve dreambooth_app.py` will [serve](https://modal.com/docs/guide/webhooks#developing-with-modal-serve) the Gradio interface at a temporary location. Great for iterating on code!
# - `modal shell dreambooth_app.py` is a convenient helper to open a bash [shell](https://modal.com/docs/guide/developing-debugging#interactive-shell) in our image. Great for debugging environment issues.
#
# Remember, once you've trained your own fine-tuned model, you can deploy it using `modal deploy dreambooth_app.py`.
#
# If you just want to try the app out, you can find our deployment [here](https://modal-labs--example-dreambooth-flux-fastapi-app.modal.run).


@app.local_entrypoint()
def run(  # add more config params here to make training configurable
    max_train_steps: int = 250,
):
    import wandb

    sweep_config = SweepConfig()
    configs = generate_sweep_configs(sweep_config)

    # print(configs[0])
    # # Use Modal's starmap to run training in parallel
    for config in train.map(configs):
        # Log summary metrics to W&B
        with wandb.init(
            project="flux-lora-sweep-heroicons-11-16-dreambooth-no-lora",
            name="sweep_summary",
        ) as run:
            evaluate_model(
                f"steps_{config['max_train_steps']}_rank_{config['rank']}",
                run,
            )