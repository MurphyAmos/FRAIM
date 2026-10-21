import json, time
import base64
from pathlib import Path
import tempfile

from google import genai

import yt_dlp
from yt_dlp.utils import DownloadError
opts = {
  "format": "bestvideo*+bestaudio/best",
  "outtmpl": "%(uploader)s/%(title)s.%(ext)s",
  "noplaylist": True,
  'merge_output_format': 'mp4',  
  "concurrent_fragment_downloads": 16,
  "quiet": True,
  
}
##if video is downloaded we can then compress the video using ffmpeg
def compress_video_subprocess(input_path, output_path=None, crf=26):
    import subprocess
    input_path = Path(input_path)
    #create an output path 
    if not output_path:
        output_path = input_path.with_name(
            input_path.stem + "_compressed.mp4"
        )

    output_path = Path(output_path)

    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output cannot be the same file")

    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", crf,
        "-c:a", "aac",
        "-b:a", "96k",
        str(output_path)
    ]
    subprocess.run(command, check=True)
    return str(output_path)

def summerize_video(PROMPT, video,boolean,client, duration =None):
    json_chunks = []
    #get chat interaction based upon interaction ID 
    def get_interaction(interaction, chunks):
        for event in interaction:
            event_type = getattr(event, "event_type", None)

            print(f"\n[EVENT] {event_type}", flush=True)

            if event_type == "error":
                print("GEMINI ERROR:", event, flush=True)
                raise RuntimeError(f"Gemini API error: {event}")

            elif event_type == "interaction.status_update":
                print("STATUS:", event, flush=True)

            elif event_type == "interaction.created":
                print("CREATED:", event, flush=True)

            elif event_type == "step.delta":
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)

                if delta_type == "thought_summary":
                    content = getattr(delta, "content", None)

                    if getattr(content, "type", None) == "text":
                        print(content.text, flush=True)

                elif delta_type == "text":
                    chunk = getattr(delta, "text", None)
                    #take final output and append to json_chuncks
                    if chunk:
                        chunks.append(chunk)
                        print(chunk, end="", flush=True)

            elif event_type in ("interaction.complete", "interaction.failed"):
                print("FINAL EVENT:", event, flush=True)

                if event_type == "interaction.failed":
                    raise RuntimeError(f"Gemini interaction failed: {event}")


    #if the video is downloaded we use the file and send that to gemini 
    if boolean != False:
      print("using the video file")
      input =  [
        {"type": "text", "text": PROMPT},
        {
            "type": "video",
            "data": base64.b64encode(open(video, "rb").read()).decode("utf-8"),
            "mime_type": "video/mp4",
             "processing": {
                "type": "static",
                "fps": 0.5,  # Sample 1 frame every 2 seconds
            },
        }
    ]
    #else we just pass the video URL to gemini directly
    else:
      print("using url")
      input=[
        {"type": "text", "text": PROMPT},
        {
            "type": "video",
            "uri": video
        }
    ]
    # Use a background execution 
    # to stop auto process canceling. 
    interaction = client.interactions.create(
      model="gemini-3.8-flash",
      input=input,
        response_format={
            "type": "text",
            "mime_type": "application/json"
        },
        background=True,
        generation_config={
            "thinking_summaries": "auto"
        },
        stream=True,
    )
    #get the interaction from above and appenened the most 
    # recent interaction to the jsonl file for possible reuse later?
    get_interaction(interaction, json_chunks)
    result_text = "".join(json_chunks).strip()

    if not result_text:
        raise ValueError("Gemini returned no JSON text")

    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Gemini returned invalid JSON: {e}\n"
            f"Response: {result_text[:1000]}"
        ) from e

    with open("data.jsonl", "a", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False) + "\n")

    return result

from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
def main(video_link, PROMPT,gemini_api_key):
    # Download the video using yt-dlp
    # sort into temp file direct
    with tempfile.TemporaryDirectory() as temp_dir:
        options = {
            **opts,
            "outtmpl": str(Path(temp_dir) / "%(id)s.%(ext)s")
        }
        
        video_downloaded = False
        # try and download video as well as pull duration 
        try:
          with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_link, download=True)
            video_path = (
                info.get("filepath")
                or ydl.prepare_filename(info)
              )
          duration_seconds = info.get('duration')
          print(duration_seconds)
          video = video_path
          #compress video for faster processing within gemini 
          video = compress_video_subprocess(
              video,
              output_path=str(Path(temp_dir) / "compressed.mp4"),
              crf=26
          )
          video_downloaded = True

        except DownloadError as e:
          video_downloaded = False
          video = video_link
          # This catches the exact 'raise DownloadError(message, exc_info)' you are seeing
          print(f"yt-dlp failed to download the video. Error context: {e}") 
        #pass our gemini client key to summerize video     
        client = genai.Client(api_key=gemini_api_key)
        return summerize_video(PROMPT, video,video_downloaded,client)

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    

    #return video link from front end 
    video_link = data.get("video_link")
    if not video_link:
        return jsonify({"error": "Missing video_link"}), 400

    #return byok token/gemini key
    gemini_api_key = data.get("gemini_api_key", "").strip()  
    if not gemini_api_key:
        return jsonify({"error": "Gemini API key required"}), 400

    prompt = r"""
      You are an expert multimodal video analyst.

      Your task is to transform audiovisual content into a detailed,
      accurate, searchable understanding of the source video.

      The output powers a video intelligence application with:
      - Expandable summary cards
      - Detailed timeline navigation
      - Searchable events and key points
      - Natural-language questions about video content

      SOURCE URL: __VIDEO_URL__

      The actual video supplied with this request is the primary
      source of evidence. The URL is an identifier, not proof that
      the video's contents have been inspected.

      OBJECTIVE

      Help a user understand the video's actual content without
      having to watch the entire recording.

      Your analysis must answer:
      - What specifically happens?
      - What is said, sung, demonstrated, or explained?
      - When do important moments occur?
      - Which details distinguish this video from others?
      - What conclusions are supported by the content?
      - How do the audio and visuals contribute to its meaning?

      Prioritize information density, specificity, and accuracy.

      Avoid generic descriptions that could apply to hundreds
      of unrelated videos.

      ANALYSIS METHOD

      1. Identify the video category and primary purpose.
      2. Inspect the accessible audiovisual content.
      3. Identify significant events, statements, actions,
        demonstrations, visual details, and transitions.
      4. Connect audio and visual observations when relevant.
      5. Distinguish observed facts from supported interpretations.
      6. Organize meaningful information chronologically.
      7. Produce an adaptive summary based on content complexity.

      Do not expose these internal steps in the JSON.

      SOURCE INTEGRITY

      - Only describe content actually observed or heard.
      - Never invent dialogue, lyrics, names, events, or timestamps.
      - Do not treat a video's claims as independently verified facts.
      - Do not identify people solely from visual appearance.
      - Do not infer exact dialogue from facial movements.
      - Do not claim complete coverage without sufficient evidence.
      - Use null for unknown metadata.
      - Use [] for unavailable collections.
      - Do not fill sections simply to meet a target count.
      - Never invent a timestamp to make the timeline look complete.

      If the source cannot be analyzed:
        analysis_status = "unavailable"

      If important portions cannot be inspected:
        analysis_status = "partial"

      If incomplete coverage materially prevents a reliable
      summary, set analysis_status = "reprocessing_required"
      and explain the problem in limitations.

      Use "complete" only when sufficient coverage of the
      source supports the analysis.

      OBSERVATION VS INTERPRETATION

      An observation describes directly visible or audible content.

      An interpretation explains meaning supported by observations.

      For every key point, distinguish its basis using:
      - "observed"
      - "interpreted"
      - "reported"

      Observed:
      Directly visible or audible information.

      Interpreted:
      A reasonable interpretation supported by video evidence.

      Reported:
      A claim made by a speaker, narrator, or other source
      within the video, not independently verified.

      Avoid psychological assumptions about people.

      VIDEO CATEGORY ADAPTATION

      Use ONE universal JSON schema for all categories.

      Adapt the content, vocabulary, and level of detail
      according to the video.

      EDUCATIONAL / TECHNICAL:
      Explain concepts, methods, examples, demonstrations,
      important details, limitations, and conclusions.

      PODCAST / INTERVIEW:
      Identify speakers when reliably established, discussion
      topics, arguments, disagreements, examples, and insights.

      NEWS / DOCUMENTARY:
      Describe reported events, sources, claims, context,
      uncertainty, and relevant chronology.

      MUSIC:
      Analyze audible themes, performance, scene changes,
      settings, visual storytelling, vocal delivery, and
      relationships between audio and imagery.

      Do not invent lyrical meaning when lyrics are unclear.
      Distinguish audible lyrical themes from interpretations
      based on visuals.

      GAMING:
      Describe objectives, actions, strategies, turning points,
      outcomes, and notable gameplay events.

      ENTERTAINMENT / VLOG:
      Describe specific events, interactions, settings,
      narrative developments, reactions, and conclusions.

      OTHER:
      Identify the content's purpose and prioritize details
      most useful to someone trying to understand it.

      OVERVIEW

      Produce:
      - A specific headline.
      - A concise preview for collapsed frontend cards.
      - A detailed summary explaining the video's content.

      The detailed summary must explain concrete events,
      topics, or developments, not merely broad themes.

      Adapt length to the video:
      - Simple short videos: approximately 70-120 words.
      - Complex short videos: approximately 120-200 words.
      - Longer complex videos: approximately 200-350 words.

      These are guidelines, not mandatory word counts.
      Use fewer words when the source provides less information.

      KEY POINTS

      Extract the most meaningful ideas, observations, events,
      or conclusions.

      Each key point includes:
      - A descriptive title
      - A short preview
      - A detailed explanation
      - Supporting timestamp when available
      - Evidence basis: observed, interpreted, or reported

      Descriptions should answer what specifically occurred,
      was explained, or was demonstrated.

      Include concrete supporting details.

      Scale the number and depth of points with the amount
      of meaningful information in the video.

      Do not repeat the same information across multiple points.

      TIMELINE

      Build a detailed chronological timeline of meaningful
      events and transitions.

      Each event includes:
      - Timestamp in seconds
      - Specific title
      - Detailed description

      Descriptions should explain what occurs at that moment.

      Include relevant:
      - Actions
      - Dialogue or vocal content
      - Visual details
      - Demonstrations
      - Scene transitions
      - Significant developments

      Do not create timestamps at regular intervals merely
      to distribute events evenly.

      Select moments based on actual significance.

      Scale timeline density with content complexity and length.

      Timestamps must reference the original source video.

      When timing is uncertain, do not invent precision.
      Use null if a reliable timestamp cannot be established.

      SEARCH SUPPORT

      The output must support natural-language retrieval.

      Use specific nouns, actions, topics, objects, and
      events in descriptions.

      A user should be able to ask questions such as:
      - When does the speaker explain the main problem?
      - Where does the artist perform on stage?
      - When is the software demonstrated?
      - What happens after the argument?
      - Where is a specific topic discussed?

      Timeline and key-point descriptions should contain
      enough concrete information to retrieve relevant moments.

      Do not generate embeddings or search results.

      A separate application component will perform retrieval.

      QUOTATIONS AND LYRICS

      Use short exact quotations only when clearly audible
      and accurately recoverable.

      Otherwise paraphrase the meaning.

      Do not reconstruct uncertain dialogue or lyrics.

      Avoid extensive verbatim reproduction of copyrighted
      lyrics or other protected material.

      TAKEAWAYS

      Extract useful conclusions or insights supported by
      the video.

      Do not repeat the overview.

      For content without meaningful conclusions, use [].

      For music and entertainment, takeaways may describe
      notable artistic or narrative observations rather than
      forced lessons.

      TOPICS

      Provide specific searchable topic labels.

      Prefer:
      "stereo camera calibration"
      over:
      "technology"

      Prefer:
      "nighttime concert performance"
      over:
      "music"

      Avoid generic labels that do not distinguish the content.

      OUTPUT FORMAT

      Return exactly ONE valid JSON object.

      No Markdown.
      No code fences.
      No commentary outside JSON.

      Use this exact schema:

      {
        "schema_version": "video_intelligence_v2",
        "analysis_status": "complete",
        "video": {
          "url": "__VIDEO_URL__",
          "title": null,
          "creator": null,
          "duration_seconds": null,
          "content_type": null,
          "language": null
        },
        "overview": {
          "headline": "",
          "preview": "",
          "summary": ""
        },
        "key_points": [
          {
            "title": "",
            "preview": "",
            "description": "",
            "timestamp_seconds": null,
            "evidence_type": "observed"
          }
        ],
        "timeline": [
          {
            "timestamp_seconds": 0,
            "title": "",
            "description": ""
          }
        ],
        "takeaways": [],
        "topics": [],
        "limitations": []
      }

      OUTPUT RULES

      - Keep all required fields.
      - Use no additional fields.
      - Preserve the specified data types.
      - Keep timeline events in chronological order.
      - Do not duplicate timeline events.
      - Do not include unsupported statements.
      - Do not invent missing metadata.
      - Use null for unavailable timestamps.
      - Use empty arrays when no reliable items exist.
      - Use specific language rather than generic summaries.
      - Ensure the result is valid JSON.

      FINAL QUALITY CHECK

      Before returning the JSON, ask internally:

      1. Could this description apply to hundreds of videos?
        If yes, replace generic language with observed details.

      2. Does each key point communicate something specific?
        If not, improve or remove it.

      3. Are the timeline events grounded in actual moments?
        If not, remove them.

      4. Are interpretations supported by observable evidence?
        If not, qualify or remove them.

      5. Does the analysis explain the relationship between
        audio and visuals where relevant?

      6. Can users locate meaningful moments through timestamps?

      7. Is the analysis depth appropriate for the actual content?

      8. Is incomplete coverage disclosed accurately?

      9. Does the returned Time-stamps align with the videos legnth?

      Return only the JSON object.
        """.replace("__VIDEO_URL__", video_link)
    #call and return main
    result = main(video_link, prompt, gemini_api_key)

    return jsonify({"result": result})

if __name__ == "__main__":
    app.run(debug=False, port=5000)
