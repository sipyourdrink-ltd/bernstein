Chào các bạn.

Tôi đã suy nghĩ một chút, và tôi nghĩ nó sẽ không giết tôi nếu, khoảng mỗi hai tuần một lần, trang Bernstein trên LinkedIn nêu tên, một cách cá nhân, từng người đóng góp muốn được nêu tên, và cảm ơn họ trong một bài đăng chung.

Một bài đăng. Không phải một bài cho mỗi người. Những người theo dõi chúng ta không cần mười bài đăng liên tiếp, và tôi cũng không cần một công việc thứ hai chỉ để viết chúng. Nhưng trong bài đăng đó, tôi muốn cố gắng nói, với từng người, rằng bạn thực sự đã làm gì, để nếu một nhà tuyển dụng tiềm năng đọc được, họ hiểu bạn đã xây dựng gì trong một dự án thực sự, và để bạn không ngại chia sẻ lại nó.

Nhân tiện, chúng ta đã bắt đầu việc này rồi. #5524 có trang LinkedIn và một loạt người trong số các bạn nói "được, cứ nêu tên tôi", và #6229 là lần thử đầu tiên gộp lời cảm ơn lại. Vậy nên đây không phải là điều gì mới. Đây chỉ là tôi cố gắng ngừng việc tự tay ghép cái này lại mỗi lần. Những ai trong số các bạn đã đồng ý ở #5524 thì không cần nói lại; tôi đã ghi nhận các bạn rồi.

## Thỏa thuận là gì

Toàn bộ quy trình, từ phía các bạn:

1. Bạn đã làm công việc có ý nghĩa trong Bernstein trong ba tháng qua.
2. Bạn theo dõi trang của dự án: https://www.linkedin.com/company/bernstein-run/
3. Thỉnh thoảng trang này sẽ đăng một bài tổng hợp nêu tên những người đã opt in và những gì họ đã làm.
4. Bạn chia sẻ lại nếu muốn, tốt nhất là gắn thẻ (tag) Bernstein.
5. Dự án có được sự tiếp cận (reach) mà nếu không thì sẽ không có. Bạn có được một hồ sơ công khai, chuyên nghiệp mà không phải do chính bạn viết về mình.

Vậy là hết. Không có bước sáu.

Nếu bạn đang tìm việc ngay lúc này, có một lựa chọn khác: tôi có thể viết cho bạn một lời giới thiệu (recommendation) trên LinkedIn thay vào đó, từ hồ sơ của tôi (https://www.linkedin.com/in/alex-chernysh/). Việc này không hề rắc rối. Nhưng tôi muốn nói thẳng về sự đánh đổi: lời giới thiệu nằm trên hồ sơ của tôi và giúp bạn; việc chia sẻ lại bài đăng chung giúp cả bạn và dự án cùng lúc. Và thật lòng, hầu như chẳng ai kéo xuống tận phần Recommendations cả. Vậy nên hãy dùng lời giới thiệu chủ yếu khi bạn đang tích cực tìm việc, và dùng việc chia sẻ lại trong các trường hợp khác. Nếu bạn muốn cả hai, cứ nói.

Về giá trị của tất cả những điều này. Nếu bạn thực sự đã làm công việc đó, đừng giấu nó đi. Đưa Bernstein vào CV của bạn, vào portfolio của bạn, bất cứ đâu nó thuộc về. Tôi làm vậy, một số bạn đã làm vậy rồi, và tôi nghĩ đó là một tín hiệu tốt: một đóng góp đã được merge vào một dự án mã nguồn mở nghiêm túc mà bạn có thể giải thích từng dòng, có giá trị hơn một dòng CV nói rằng "biết Python". Đó là ý kiến của tôi, không phải một nghiên cứu. Nó cũng không phải là một lời hứa về bất cứ điều gì: đây là một dự án tự nguyện, không ai đang tuyển dụng cả, và tôi thích nói điều này rõ ràng ngay từ bây giờ hơn là để ai đó sau này đọc ra một điều gì khác từ nó.

## Cách để nói đồng ý

Gửi một email. Địa chỉ và chủ đề (subject) đúng như thế này:

> **To:** forte@bernstein.run
> **Subject:** `BRNSTN-PR-LNKD`

Trong nội dung, viết vài dòng mà một script có thể đọc được:

```
GITHUB=your-github-login
OPT_IN=YES
RECOMMENDATION=NO
```

Đặt RECOMMENDATION=YES nếu bạn muốn lời giới thiệu LinkedIn thay cho, hoặc cùng với, bài đăng. Thêm một dòng NOTE= nếu có điều gì tôi nên biết (ví dụ bạn muốn xem qua nội dung trước khi nó được đăng; một vài người đã yêu cầu vậy và điều đó hoàn toàn được). Để opt out sau này, cùng địa chỉ, cùng chủ đề, OPT_IN=NO.

Xin đừng đổi dòng chủ đề (subject line). Bộ phân tích (parser) cần đúng chuỗi đó. Lượng email gửi đến nhiều đến mức đây không còn là vấn đề sở thích; một email với chủ đề khác sẽ rơi vào hàng chờ chung và tôi không thể hứa là sẽ bao giờ thấy nó.

## Ngưỡng lọc mềm, và vì sao nó mềm

Để quyết định ai vào một bài đăng cụ thể mà không cần tôi đọc từng PR bằng tay, có một bộ lọc máy móc: khoảng 1.000 dòng được thêm vào trong các pull request đã merge trong 30 ngày qua, không tính các file được sinh tự động (generated files). Đây không phải là một KPI. Không phải là một chỉ số chất lượng. Không phải là một sự đánh giá về ai cả. Đây chỉ là một cách rẻ để một script tập hợp một nhóm (cohort) mỗi vài tuần mà không cần con người tham gia.

Và nó cũng không phải một hàng rào cứng nhắc. Ở đây có người làm công việc bảo mật, review, viết tài liệu, benchmark, các việc vặt liên quan đến release và debug, những việc có thể vô cùng giá trị chỉ với bốn mươi dòng diff. Nếu bạn đã làm công việc có ý nghĩa và vì lý do nào đó chưa đạt đến một nghìn dòng, hãy viết đến cùng địa chỉ, cùng chủ đề và nói vậy. Chúng ta không phải là quái vật; chúng ta sẽ tìm ra cách. Câu trả lời thường gặp là chúng tôi tìm cho bạn công việc tiếp theo và bạn sẽ vượt qua con số một nghìn không may đó ở lần đóng góp kế tiếp.

Tên trong danh sách định kỳ được xếp theo thứ tự chữ cái (alphabet). Không theo số lượng PR, không theo việc tôi thích ai hơn. Theo thứ tự chữ cái.

## GitHub Discussions và trang LinkedIn không cạnh tranh với nhau

Chúng làm những việc khác nhau. Với tin tức nhanh (một bản release, một thay đổi, một cập nhật cộng đồng), trang LinkedIn là kênh nhanh hơn. GitHub Discussions là nơi bạn tham gia vào dự án một cách thực sự: bạn thấy được bối cảnh (context), bạn tranh luận, bạn đề xuất, bạn tìm ra câu trả lời, và nó được lưu lại để người tiếp theo có thể tìm thấy. Nếu bạn muốn cả tin tức và chiều sâu, hãy theo dõi cả hai. Các quyết định vẫn diễn ra trong issue và pull request; điều đó không thay đổi.

## Vì sao tôi làm tất cả những điều này

Rất nhiều bạn còn trẻ. Một số bạn thì không hẳn còn trẻ nữa. Nhưng đối với một người mới bắt đầu, một dự án mã nguồn mở thực sự là một cơ hội khá hiếm để thử không chỉ viết code mà còn review, kiến trúc (architecture), kiểm thử, viết tài liệu, bảo mật, phát hành (release), điều phối, thậm chí cả một chút viết lách công khai, và để đưa ra những quyết định có hậu quả thật. Nếu tôi có thể làm việc trên một thứ như thế này khi còn học đại học, cuộc đời tôi có lẽ đã diễn ra khá khác. Vì vậy tôi muốn ủng hộ những người mới bắt đầu xây dựng nhiều nhất có thể.

Mô hình của tôi rất đơn giản: cắt các mốc quan trọng (milestone) thành những miếng nhỏ mà một người có thể cầm nắm được, giao cho ai đó một công việc thực sự, để quyết định thuộc về họ ở bất cứ nơi nào có thể, không lấy lại công việc sau lần sai lầm đầu tiên, và để mọi người nhận thêm trách nhiệm khi họ tiến bộ.

Với những bạn đang học một thứ gì đó liên quan đến CS ngay lúc này, hoặc chỉ mới bắt đầu: tôi thấy các bạn. Tôi ở cùng các bạn. Các bạn đang làm tốt.

Một ý kiến nữa, được ghi rõ là ý kiến. Biết viết code đang trở thành một điều cơ bản, như biết đánh máy. Biết suy nghĩ, nhìn ra sự đánh đổi (trade-off), đưa ra một quyết định mà bạn có thể bảo vệ được, điều đó vẫn còn hiếm, và trong thời đại AI tôi nghi là nó sẽ càng hiếm hơn và giá trị hơn, không phải ít hơn. Đây là nơi để luyện tập chính xác điều đó.

## Dự án đang được vận hành thế nào, cho đến giờ

Cho đến hết năm dương lịch này, tôi vẫn là người duy trì (maintainer) duy nhất. Đó là có chủ đích. Điều đó không có nghĩa là maintainer quyết định mọi thứ. Quyền sở hữu là của tôi; tôi không nghĩ mình nên tự tay chọn từng dấu phẩy của kiến trúc. Nếu bạn đến để tạo một PR, tôi muốn bạn suy nghĩ, không phải đoán maintainer muốn gì.

Khi tôi có năng lực, và ngay lúc này tôi đang bị chôn khá sâu trong những việc khác, tôi muốn thử làm cho công việc có hình dạng hơn một chút, với các "phòng ban" ảo theo cách một công ty bình thường có. Tất cả đều tự nguyện, không có nghĩa vụ, không có bộ máy quan liêu vì lợi ích của riêng nó. Để bắt đầu tôi chỉ thấy hai: R&D, thứ đã sống trong issue và pull request và cần rất ít từ tôi, và PR / outreach (đối ngoại), thứ mà tôi có thể sẽ giám sát sát sao hơn một chút, vì giao tiếp không có cấu trúc biến thành "chúng ta nên làm việc đó lúc nào đó" nhanh hơn bất cứ điều gì khác tôi biết.

Sau này, nếu có đủ người hoạt động tích cực, các vai trò có thể xuất hiện: một giám đốc phát triển, một cho vận hành, một cho bảo mật, một cho QA và tài liệu. Tôi không tạo ra những chức danh đó trước. Số lượng người quyết định cấu trúc, không phải ngược lại. Không ai cần một tập đoàn sáu người.

## Tiền, vì luôn có người hỏi

Khi ai đó hỏi liệu có cơ hội trả lương để làm việc trên Bernstein không, đôi khi tôi bật ra một tiếng cười nhỏ, hơi lo lắng. Bernstein không kiếm ra tiền. Nó tiêu tiền của tôi. Tôi thậm chí không đếm thời gian của mình; tôi chỉ nhìn xem tiền rời khỏi tài khoản ngân hàng ra sao, ở một đất nước khá đắt đỏ, trong khi tôi đang ở giữa các công việc và hoàn cảnh có lẽ sắp cho tôi thêm ít nhất vài tuần nữa trên dự án này. Vậy nên điều tôi đang làm không phải là công việc không lương. Đó là công việc thua lỗ. Tôi thích công việc này. Đó, nói chung, là lý do tại sao tất cả chúng ta ở đây.

Miễn phí cho người dùng không có nghĩa là miễn phí cho người duy trì. Cơ sở hạ tầng phía sau việc này tốn khoảng vài nghìn đô la mỗi tháng, và hóa đơn đó vẫn đến bất kể có ai đăng gì trên LinkedIn hay không.

Về giấy phép (licence), nói một cách chính xác, vì có lần tôi đã hiểu sai điều này trong đầu mình: Bernstein là Apache-2.0. Apache-2.0 cho phép sử dụng thương mại; nó không buộc ai phải phi thương mại. Việc Bernstein đã, đang, và theo tôi thì sẽ vẫn là một dự án mã nguồn mở tự do là một lập trường và một ý định, không phải một điều khoản của giấy phép.

Một dự án tự do vẫn có thể có nhà tài trợ, quảng cáo, các vị trí hợp tác (partnership), các tích hợp liên quan. Nếu bạn biết một AI lab hay một công ty trong lĩnh vực này mà điều đó thực sự có ý nghĩa, hãy chỉ họ đến tôi. Tôi sẽ cố gắng chuẩn bị một gói đối tác (partner package) đúng nghĩa trong vài tuần tới, và rồi chúng ta sẽ xem có thể làm gì với nó. Cho đến lúc đó thì có GitHub Sponsors, thứ đang tồn tại và còn nhỏ.

Một quy tắc chắc chắn. Không có tích hợp trả tiền nào từng mua được một lần merge. Nếu một công ty muốn trả tiền cho một công việc tích hợp cụ thể, công việc đó vẫn đi qua quy trình kỹ thuật bình thường, người duy trì và cộng đồng vẫn giữ quyền nói không, và một quyết định kỹ thuật không thay đổi chỉ vì xuất hiện một khoản ngân sách. Nếu có bao giờ một công việc trả tiền thực sự được chấp thuận, số tiền thu được sẽ dành cho hosting, chi phí vận hành và những người duy trì dự án. Đó không phải là một chính sách trả công; không có tỷ lệ phần trăm nào; đó chỉ là tiền sẽ đi đâu.

Và một điều mang tính con người nữa. Nếu một cơ hội trả lương thực sự từng xuất hiện xung quanh Bernstein, những người đã bỏ công sức vào rồi và những người tôi đã biết rõ rồi rõ ràng sẽ là những người đầu tiên tôi xem xét. Đó không phải là một lời hứa, không phải một chương trình, và không phải là "làm việc miễn phí bây giờ, được thuê sau". Đó chỉ là logic thông thường rằng nếu tôi đã thích cách một người suy nghĩ và làm việc, tôi sẽ nhớ đến họ trước một người lạ.

## Ai đang theo dõi, và vì sao điều đó quan trọng

Tất nhiên chúng ta không thể công khai toàn bộ hệ thống phân tích của mình, nhưng theo mọi dấu hiệu, chúng ta không phải là những người duy nhất đang đọc. Người từ các công ty IT lớn và các AI lab xuất hiện trên trang web khá thường xuyên và đặt những câu hỏi khá cụ thể về tài liệu. Các đối thủ cạnh tranh, cứ giả định vậy, cũng không ngủ.

Và, nhân tiện: theo phân tích thụ động (passive analytics) của chúng tôi, sau khi loại bỏ nhiễu bot rõ ràng, Bernstein hiện đang chạy đều đặn trên ít nhất khoảng 3.000 máy trên toàn thế giới. Tôi nghĩ đó là một con số khá đáng nể. Bernstein theo mặc định không gửi bất kỳ dữ liệu đo từ xa (telemetry) nào về sản phẩm, và con số này không đến từ bất kỳ dữ liệu đó; đó là một tín hiệu thụ động, và đó là tất cả những gì tôi sẽ nói về phương pháp này.

Vậy nên tình hình đã khá thú vị rồi. Chúng ta đang xây dựng một sản phẩm miễn phí mà người ta thực sự sử dụng, nó thu hút sự chú ý của các kỹ sư, các AI lab và các công ty lớn, và bất kỳ người dùng nào cũng có thể lấy mã nguồn và làm chủ nó. Ý kiến của tôi, rõ ràng là một ý kiến: khi bạn có thể có được một công cụ nghiêm túc miễn phí và làm chủ mã nguồn, sẽ khó giải thích tại sao ai đó lại trả hàng chục nghìn đô la mỗi tháng cho một thứ tương tự.

Tôi muốn dự án này cảm thấy lớn. Không phải để chúng ta có thể nói với mọi người rằng chúng ta tuyệt vời, mà để mọi người làm việc ở đây hiểu rằng họ đang làm điều gì đó có thể quan trọng vượt xa một pull request. Ngưỡng trong đầu tôi đại khái là Ansible, Kubernetes, Terraform. Không phải "Bernstein là Kubernetes tiếp theo". Mà đúng hơn là: nếu Bernstein có ngày nào đó trở thành cách triển khai tham chiếu (reference implementation) trong lĩnh vực của nó, tôi không muốn chúng ta nhìn lại và nghĩ rằng chúng ta đã làm nó một cách hấp tấp. Chúng ta nên làm nó tốt.

## Vài điều thành thật cần nói trước

Tôi không thể hứa về nhịp độ. "Khoảng mỗi hai tuần một lần" là một mục tiêu, không phải một mức dịch vụ (service level). Hoàn toàn có thể là bài đăng đầu tiên sẽ đến hạn trong hai tuần, rồi một tháng trôi qua, rồi hóa ra chúng ta muốn tự động hóa thêm ba thứ nữa trước, và mọi thứ tưởng chỉ mất vài tuần lại lặng lẽ kéo dài đến hết năm dương lịch. Chúng tôi sẽ cố gắng. Tôi sẽ không hứa với bạn điều mà tôi không chắc mình có thể thực hiện.

Khoảng giữa tháng Mười Một, nếu trời cho phép và năng lực cho phép, tôi muốn tổ chức một cuộc gọi Zoom ngắn. Chỉ để gặp mặt, và để nói về việc mỗi bạn muốn đi đâu. Không có nghĩa vụ phải tham gia, chưa có ngày cụ thể.

Chúng tôi tôn trọng việc mọi người trong dự án này nói những ngôn ngữ khác nhau, vì vậy bản gốc vẫn ở tiếng Anh và một vài bản dịch sẽ theo sau ở phần bình luận dưới đây. Đây là một cố gắng nhỏ để làm mọi thứ dễ chịu hơn một chút cho các bạn, không hơn không kém.

Về phía tôi, đây chủ yếu là tôi và con mèo. Về phía các bạn, đó là tất cả những người còn lại.

Chia sẻ là quan tâm (sharing is caring). Tôi cố gắng cho lại các bạn nhiều nhất tôi có thể, vì công việc của các bạn, và chỉ vì các bạn có mặt ở đây. Hãy tin tôi, tôi rất quý tất cả các bạn. Trong số những điều khác, đó là thứ khiến tôi thức dậy vào buổi sáng và ngồi trước máy tính, sau đó tôi phát hiện ra mười lăm giờ đã trôi qua. Hãy xem chúng ta sẽ xây dựng được gì từ tất cả những điều này.

Alex
