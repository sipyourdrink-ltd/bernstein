{{LANGUAGE_SWITCHER}}

Friends, hello.

I have been thinking a bit, and I think it will not kill me if, roughly once every couple of weeks, the Bernstein page on LinkedIn names, personally, every contributor who wants to be named, and thanks them in one shared post.

One post. Not one per person. Our followers do not need ten posts in a row, and I do not need a second job writing them. But inside that one post I want to try to say, per person, what you actually did, so that if a potential employer reads it they understand what you built in a real project, and so that you are not embarrassed to repost it.

We started this already, by the way. #5524 has the LinkedIn page and a bunch of you saying "fine, name me", and #6229 was the first attempt at a consolidated thank-you. So this is not something new. It is me trying to stop assembling the thing by hand every time. Those of you who already said yes in #5524 do not need to say it again; I have you.

## What the deal is

The whole flow, from your side:

1. You did meaningful work in Bernstein in the last three months.
2. You follow the project page: https://www.linkedin.com/company/bernstein-run/
3. Now and then the page publishes one consolidated post naming the people who opted in and what they did.
4. You repost it if you want to, ideally tagging Bernstein.
5. The project gets reach it would not otherwise get. You get a public, professional record that is not written by you about yourself.

That is it. There is no step six.

If you are job-hunting right now, there is an alternative: I can write you a LinkedIn recommendation instead, from my profile (https://www.linkedin.com/in/alex-chernysh/). It is no trouble. But I want to be straight about the trade: the recommendation sits on my profile and helps you; the repost of the shared post helps you and the project at the same time. And honestly, almost nobody scrolls down to the Recommendations section anyway. So please use the recommendation mostly if you are actively looking, and the repost otherwise. If you want both, say so.

On the value of any of this. If you really did the work, do not hide it. Put Bernstein in your CV, in your portfolio, wherever it belongs. I do, some of you already do, and I think it is a good signal: a merged contribution to a serious open-source project that you can explain, line by line, is worth more than a résumé line that says "knows Python". That is my opinion, not a study. It is also not a promise of anything: this is a volunteer project, nobody is hiring, and I would rather say that plainly now than let anyone read something into it later.

## How to say yes

Send one email. Address and subject exactly like this:

> **To:** forte@bernstein.run
> **Subject:** `BRNSTN-PR-LNKD`

In the body, a few lines a script can read:

```
GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Set `RECOMMENDATION=YES` if you want the LinkedIn recommendation instead of, or as well as, the post. Add a `NOTE=` line if there is something I should know (for example that you want to see the wording before it goes out; a couple of you asked for that and it is fine). To opt out later, same address, same subject, `OPT_IN=NO`.

Please do not change the subject line. The parser likes exactly that string. The volume of incoming mail is such that this is not a matter of taste; a message with a different subject lands in the general queue and I cannot promise I will ever see it.

{{REPLY_BY}}

## The soft gate, and why it is soft

To decide who goes into a given post without me reading every PR by hand, there is a mechanical filter: roughly 1,000 added lines in merged pull requests over the last 30 days, generated files excluded. It is not a KPI. It is not a quality metric. It is not a judgement of anyone. It is a cheap way for a script to assemble a cohort every couple of weeks without a human in the loop.

And it is not a concrete fence. People here do security work, reviews, docs, benchmarks, release plumbing and debugging that can be enormously valuable at forty lines of diff. If you did meaningful work and did not get to a thousand for whatever reason, write to the same address with the same subject and say so. We are not monsters; we will figure something out. The usual answer is that we find you the next task and you cross the unfortunate thousand with the next contribution.

Names in the periodic list go in alphabetical order. Not by number of PRs, not by whom I like more. Alphabetical.

## GitHub Discussions and the LinkedIn page are not competing

They do different jobs. For fast news (a release, a change, a community update) the LinkedIn page is the quicker channel. GitHub Discussions is where you take part in the project properly: you see the context, you argue, you propose, you find the answer, and it stays on record where the next person can find it. If you want both the news and the depth, follow both. Decisions still happen in issues and pull requests; that has not changed.

## Why I am doing this at all

A lot of you are young. Some of you are considerably not. But for someone starting out, a real open-source project is a fairly rare chance to try not only code but review, architecture, testing, documentation, security, releases, coordination, even a bit of public writing, and to make decisions that have consequences. If I could have worked on something like this while I was at university, my life would probably have turned out rather differently. So I want to back beginning builders as much as I can.

My model is simple: cut milestones into pieces a person can hold, give someone a real task, leave the decision to them wherever it can be theirs, do not take the task away after the first mistake, and let people take on more responsibility as they go.

To those of you who are studying something CS-shaped right now, or just starting: I see you. I am with you. You are doing well.

One more opinion, clearly labelled as such. Being able to code is becoming a baseline, like being able to type. Being able to think, to see the trade-off, to make a decision you can defend, that is still rare, and in the AI era I suspect it gets rarer and more valuable, not less. This is a place to practise exactly that.

## How the project is run, for now

Until the end of this calendar year I stay the only maintainer. That is deliberate. It does not mean the maintainer decides everything. Ownership is mine; I do not think I should personally pick every comma of the architecture. If you came to make a PR, I want you to think, not to guess what the maintainer wants.

When I have the capacity, and right now I am somewhat buried in other things, I want to try making the work a little more shaped, with virtual "departments" the way an ordinary company has them. All voluntary, no obligations, no bureaucracy for its own sake. To begin with I see only two: R&D, which already lives in issues and pull requests and needs little from me, and PR / outreach, which I will probably supervise a bit more closely, because communication without structure turns into "we should do that sometime" faster than anything else I know.

Later, if there are enough active people, roles might follow: a director for development, one for operations, one for security, one for QA and docs. I am not creating those titles in advance. The number of people determines the structure, not the other way round. Nobody needs a corporation of six.

## Money, since somebody always asks

When someone asks whether there is a paid opportunity to work on Bernstein, I sometimes laugh a small nervous laugh. Bernstein does not make money. It spends mine. I do not even count my time; I just watch what leaves the bank account, in a fairly expensive country, while I am between jobs and circumstances are probably about to hand me at least another couple of weeks on this project. So what I am doing is not unpaid work. It is work at a loss. I like the work. That is, broadly, why we are all here.

Free for the user does not mean free for the maintainer. The infrastructure behind this costs on the order of a couple of thousand dollars a month, and that bill arrives whether or not anyone posts anything on LinkedIn.

On licences, precisely, because I got this wrong in my own head once: Bernstein is Apache-2.0. Apache-2.0 permits commercial use; it does not oblige anyone to be non-commercial. That Bernstein was, is, and as far as I am concerned stays a free open-source project is a position and an intention, not a term of the licence.

A free project can still have sponsors, advertising, partnership placements, relevant integrations. If you know an AI lab or a company in this space for whom that could genuinely make sense, point them at me. I will try to get a proper partner package in order in the next few weeks, and then we will see what can be done with it. Until then there is GitHub Sponsors, which exists and is small.

One firm rule. No paid integration ever buys a merge. If a company wants to pay for a specific piece of integration work, the work goes through the ordinary technical process, the maintainer and the community keep the right to say no, and a technical decision does not change because a budget appeared. If some paid work is ever actually approved, the proceeds would go towards hosting, running costs and the people who carry the project. That is not a compensation policy; there are no percentages; it is where the money would go.

And one more human thing. If a real paid opportunity ever shows up around Bernstein, the people who already put work in and whom I already know will obviously be among the first I look at. That is not a promise, not a programme, and not "work for free now, get hired later". It is only the ordinary logic that if I already like how someone thinks and works, I will remember them before a stranger.

## Who is watching, and why it matters

We cannot, of course, publish our whole analytics pipeline, but by all appearances we are not the only ones reading. People from large IT companies and AI labs turn up on the site fairly regularly and ask quite specific questions of the documentation. The competitors, let us assume, are not asleep either.

And, by the way: by our passive analytics, with the obvious bot noise removed, Bernstein now runs regularly on at least around 3,000 machines around the world. I think that is fairly respectable. Bernstein sends no product telemetry by default and this number does not come from any; it is a passive signal, and that is all I will say about the method.

So the situation is already an interesting one. We are building a free product that people actually use, it draws the attention of engineers, AI labs and large companies, and any user can take the source and own it. My opinion, clearly an opinion: when you can get a serious tool for free and own the code, it becomes hard to explain why anyone would pay tens of thousands of dollars a month for something similar.

I want this project to feel big. Not so we can tell people we are great, but so that everyone working here understands they are doing something that may matter well beyond one pull request. The bar in my head is roughly Ansible, Kubernetes, Terraform. Not "Bernstein is the next Kubernetes". Rather: if Bernstein ever becomes the reference implementation in its category, I do not want us looking back and thinking we did it in a hurry. We should do it well.

## A few honest caveats

I cannot promise the cadence. "Roughly once every couple of weeks" is a goal, not a service level. It is entirely possible that the first post is due in two weeks, then a month passes, then it turns out we wanted to automate three more things first, and everything that was going to take weeks quietly takes until the end of the calendar year. We will try. I will not promise you what I am not sure I can deliver.

Around mid-November, God willing and capacity permitting, I would like to do a short Zoom call. Just to meet, and to talk about where each of you wants to go. No obligation to attend, no exact date yet.

We respect that people in this project speak different languages, so the original stays in English and a few translations follow below in the comments. It is a small attempt to make things a bit nicer for you, nothing more.

On my side, this is mostly me and the cat. On your side, it is all the rest of you.

Sharing is caring. I try to give you as much back as I can for your work and simply for being here. Believe me, I am very fond of all of you. Among other things it is what gets me up in the morning and in front of the computer, after which I discover that fifteen hours have passed. Let us see what we build out of all this.

Alex
